# Build Swarm API Reference

**Version 0.4.0-alpha**

Complete API documentation for Gentoo Build Swarm.

---

## Overview

Build Swarm exposes two HTTP servers:

| Port | Name | Purpose | Authentication |
|------|------|---------|----------------|
| 8100 | Public API | Status, drone registration, work requests | None (read-only) / Admin key (write) |
| 8093 | Admin API | Full control, logs, payloads | `X-Admin-Key` header required |

### Authentication

Protected endpoints require the `X-Admin-Key` header:

```bash
curl -H "X-Admin-Key: your-secret-key" http://localhost:8100/api/v1/queue
```

Set the key via environment variable:
```bash
export ADMIN_KEY="your-secret-key"
```

### Response Format

All responses are JSON:

```json
{
  "status": "ok",
  "data": { ... }
}
```

Error responses:
```json
{
  "error": "Description of error",
  "hint": "How to fix it"
}
```

---

## Public API (Port 8100)

### Health & Status

#### GET /api/v1/health

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "version": "0.4.0-alpha",
  "uptime_s": 3600.5
}
```

#### GET /api/v1/status

Queue and session status overview.

**Response:**
```json
{
  "version": "0.4.0-alpha",
  "nodes": 4,
  "nodes_online": 3,
  "total_cores": 62,
  "queue_depth": 150,
  "queue_received": 45,
  "queue_blocked": 2,
  "needed": 100,
  "delegated": 5,
  "paused": false,
  "session": {
    "id": "session-20260212-143000",
    "name": "World rebuild",
    "status": "active",
    "total_packages": 200,
    "completed_packages": 45,
    "failed_packages": 2
  },
  "timing": {
    "total_builds": 1234,
    "success_rate": 95.5,
    "avg_duration_s": 180,
    "total_duration_s": 222120,
    "failed": 55
  },
  "drones": {
    "drone-01": {
      "name": "drone-01",
      "ip": "10.0.0.175",
      "status": "online",
      "cores": 16,
      "current_task": "dev-libs/openssl-3.2.0",
      "assigned_packages": ["dev-libs/openssl-3.2.0"]
    }
  }
}
```

### Nodes

#### GET /api/v1/nodes

List registered drones.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `all` | boolean | false | Include offline drones |
| `format` | string | | Use `legacy` for v2-compatible format |

**Response:**
```json
[
  {
    "id": "abc123...",
    "name": "drone-01",
    "ip": "10.0.0.175",
    "tailscale_ip": "100.92.27.91",
    "type": "drone",
    "drone_type": "lxc",
    "cores": 16,
    "ram_gb": 32.0,
    "status": "online",
    "paused": false,
    "last_seen": 1707745234.5,
    "version": "0.4.0-alpha",
    "cpu_percent": 45.2,
    "ram_percent": 62.1,
    "assigned_packages": ["dev-libs/openssl-3.2.0"],
    "build_progress": [
      {
        "package": "dev-libs/openssl-3.2.0",
        "building_since": 1707745000,
        "elapsed_s": 234.5,
        "estimated_s": 300,
        "progress_pct": 78
      }
    ],
    "builds_completed": 150,
    "builds_failed": 5
  }
]
```

### Events

#### GET /api/v1/events

Recent events from ring buffer.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 200 | Maximum events to return |
| `since` | float | | Unix timestamp filter |
| `type` | string | | Event type filter |

**Response:**
```json
{
  "events": [
    {
      "id": 1234,
      "timestamp": 1707745234.5,
      "type": "complete",
      "message": "drone-01 completed dev-libs/openssl-3.2.0 in 5m 23s",
      "details": {
        "drone_id": "abc123",
        "package": "dev-libs/openssl-3.2.0",
        "duration_s": 323
      }
    }
  ]
}
```

**Event Types:**
| Type | Description |
|------|-------------|
| `register` | Drone came online |
| `complete` | Build completed successfully |
| `fail` | Build failed |
| `grounded` | Drone grounded due to failures |
| `stale` | Drone went offline |
| `control` | Control action executed |
| `escalation` | Self-healing escalation |

#### GET /api/v1/events/history

Persistent event log from SQLite.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 500 | Maximum events |
| `since` | float | | Unix timestamp filter |
| `type` | string | | Event type filter |
| `drone` | string | | Drone name filter |

### History

#### GET /api/v1/history

Build history.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 100 | Maximum records |
| `status` | string | | Filter by status |
| `drone` | string | | Filter by drone name |

**Response:**
```json
{
  "builds": [
    {
      "id": 5678,
      "package": "dev-libs/openssl-3.2.0",
      "drone_id": "abc123",
      "drone_name": "drone-01",
      "status": "success",
      "duration_seconds": 323.5,
      "error_message": null,
      "session_id": "session-20260212",
      "built_at": 1707745234.5
    }
  ]
}
```

### Sessions

#### GET /api/v1/sessions

List build sessions.

**Response:**
```json
{
  "sessions": [
    {
      "id": "session-20260212-143000",
      "name": "World rebuild",
      "status": "active",
      "total_packages": 200,
      "completed_packages": 45,
      "failed_packages": 2,
      "started_at": 1707740000,
      "completed_at": null
    }
  ]
}
```

---

## Protected Public Endpoints

These endpoints on port 8100 require `X-Admin-Key` header.

### Queue Management

#### POST /api/v1/queue

Add packages to the build queue.

**Request:**
```json
{
  "packages": ["dev-libs/openssl", "sys-apps/portage"],
  "session_name": "Security updates"
}
```

**Response:**
```json
{
  "status": "ok",
  "queued": 2,
  "packages": ["dev-libs/openssl-3.2.0", "sys-apps/portage-2.3.99"],
  "session_id": "session-20260212-150000"
}
```

### Control Actions

#### POST /api/v1/control

Execute a control action.

**Request:**
```json
{
  "action": "pause"
}
```

**Actions:**
| Action | Description |
|--------|-------------|
| `pause` | Pause build queue |
| `resume` | Resume build queue |
| `unblock` | Unblock all blocked packages |
| `unground` | Clear grounded state on all drones |
| `reset` | Reset queue to initial state |
| `rebalance` | Reclaim delegated packages |
| `clear_failures` | Clear all failure states |
| `retry_failures` | Re-queue failed packages |

**Response:**
```json
{
  "status": "ok",
  "action": "pause",
  "message": "Queue paused"
}
```

### Drone Control

#### POST /api/v1/nodes/{name}/pause

Pause a specific drone.

**Response:**
```json
{
  "status": "ok",
  "drone": "drone-01",
  "paused": true
}
```

---

## Self-Healing API (Port 8100)

### GET /api/v1/ping

Get ping status for all drones.

**Response:**
```json
{
  "drones": {
    "drone-01": {
      "last_ping_at": 1707745230,
      "last_pong_at": 1707745231,
      "ping_latency_ms": 23.5,
      "status": "ok"
    }
  }
}
```

### GET /api/v1/ping/all

Trigger ping to all online drones.

**Response:**
```json
{
  "status": "ok",
  "pinged": 3,
  "drones": ["drone-01", "drone-02", "drone-03"]
}
```

### GET /api/v1/escalation

Get escalation status for all drones.

**Response:**
```json
{
  "monitor_running": true,
  "drones": {
    "drone-01": {
      "name": "drone-01",
      "status": "online",
      "drone_type": "lxc",
      "escalation_level": 0,
      "last_escalation_at": null,
      "last_ping_at": 1707745230,
      "ping_latency_ms": 23.5
    },
    "drone-02": {
      "name": "drone-02",
      "status": "offline",
      "drone_type": "qemu",
      "escalation_level": 2,
      "last_escalation_at": 1707745200,
      "last_ping_at": 1707745100,
      "ping_latency_ms": null
    }
  }
}
```

### POST /api/v1/nodes/{name}/ping (Admin)

Ping a specific drone.

**Response:**
```json
{
  "status": "ok",
  "drone": "drone-01",
  "latency_ms": 23.5,
  "load": 2.4,
  "disk_percent": 45
}
```

### POST /api/v1/nodes/{name}/reset-escalation (Admin)

Reset escalation level for a drone.

**Response:**
```json
{
  "status": "ok",
  "drone": "drone-01",
  "previous_level": 2,
  "current_level": 0
}
```

### POST /api/v1/nodes/{name}/set-type (Admin)

Set the drone type (for safe reboot handling).

**Request:**
```json
{
  "drone_type": "lxc"
}
```

**Valid Types:**
- `lxc` - LXC container (safe to reboot)
- `qemu` - QEMU/KVM VM (safe to reboot)
- `bare-metal` - Physical machine (NEVER auto-reboot)
- `unknown` - Not set (will not auto-reboot)

**Response:**
```json
{
  "status": "ok",
  "drone": "drone-01",
  "drone_type": "lxc"
}
```

---

## Admin API (Port 8093)

All admin endpoints require `X-Admin-Key` header.

### System Information

#### GET /admin/api/system/info

System information and configuration.

**Response:**
```json
{
  "version": "0.4.0-alpha",
  "uptime_s": 3600,
  "uptime_human": "1h 0m",
  "db_path": "/var/lib/build-swarm-v3/swarm.db",
  "db_size_mb": 45.2,
  "control_plane_port": 8100,
  "admin_port": 8093,
  "v2_gateway_url": "http://10.0.0.204:5000",
  "binhost_primary_ip": "10.0.0.204",
  "binhost_secondary_ip": "100.114.16.118"
}
```

### Logs

#### GET /admin/api/drones/{name}/syslog

View drone system logs.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lines` | int | 100 | Number of lines |

**Response:**
```json
{
  "drone": "drone-01",
  "lines": [
    "Feb 12 15:30:01 drone-01 swarm-drone[1234]: Heartbeat sent",
    "Feb 12 15:30:05 drone-01 emerge[5678]: Building dev-libs/openssl"
  ]
}
```

#### GET /admin/api/logs/control-plane

View control plane logs.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lines` | int | 200 | Number of lines |

**Response:**
```json
{
  "path": "/var/log/build-swarm-v3/control-plane.log",
  "lines": [
    "2026-02-12 15:30:01 INFO [control_plane] drone-01 registered",
    "2026-02-12 15:30:05 INFO [scheduler] Assigned dev-libs/openssl to drone-01"
  ]
}
```

### Self-Healing

#### GET /admin/api/self-healing/status

Self-healing monitor status.

**Response:**
```json
{
  "monitor_running": true,
  "probe_interval_s": 30,
  "last_probe_at": 1707745230,
  "escalation_summary": {
    "level_0": 3,
    "level_1": 0,
    "level_2": 1,
    "level_3": 0,
    "level_4": 0
  }
}
```

#### GET /admin/api/drones/{name}/escalation

Detailed escalation info for a drone.

**Response:**
```json
{
  "drone": "drone-01",
  "status": "online",
  "drone_type": "lxc",
  "escalation_level": 0,
  "last_escalation_at": null,
  "escalation_attempts": 0,
  "last_probe_result": {
    "status": "ok",
    "load": 2.4,
    "disk_percent": 45,
    "emerge_running": true
  },
  "last_probe_at": 1707745230
}
```

#### POST /admin/api/drones/{name}/ping

Trigger proof-of-life ping.

**Response:**
```json
{
  "status": "ok",
  "drone": "drone-01",
  "latency_ms": 23.5,
  "probe_result": {
    "load": 2.4,
    "disk_percent": 45,
    "emerge_running": true
  }
}
```

### Payload Versioning

#### GET /admin/api/payloads

List all payload versions.

**Response:**
```json
{
  "versions": [
    {
      "id": 1,
      "payload_type": "drone_binary",
      "version": "0.4.0",
      "hash": "sha256:abc123...",
      "content_path": "/var/lib/build-swarm-v3/payloads/drone_binary-0.4.0",
      "description": "Swarm drone v0.4.0",
      "notes": "Self-healing support",
      "created_at": 1707745000,
      "created_by": "admin"
    }
  ]
}
```

#### POST /admin/api/payloads

Register a new payload version.

**Request:**
```json
{
  "type": "drone_binary",
  "version": "0.4.0",
  "content": "<base64-encoded-content>",
  "description": "Swarm drone v0.4.0",
  "notes": "Self-healing support",
  "created_by": "admin"
}
```

**Response:**
```json
{
  "status": "ok",
  "version": {
    "id": 1,
    "payload_type": "drone_binary",
    "version": "0.4.0",
    "hash": "sha256:abc123..."
  }
}
```

#### GET /admin/api/payloads/status

Deployment status matrix.

**Response:**
```json
{
  "payload_types": ["drone_binary", "init_script", "config"],
  "latest_versions": {
    "drone_binary": {
      "version": "0.4.0",
      "hash": "abc123...",
      "created_at": 1707745000
    }
  },
  "drones": {
    "drone-01": {
      "drone_binary": {
        "version": "0.4.0",
        "status": "deployed",
        "is_current": true
      }
    },
    "drone-02": {
      "drone_binary": {
        "version": "0.3.2",
        "status": "deployed",
        "is_current": false
      }
    }
  },
  "outdated_count": 1
}
```

#### GET /admin/api/payloads/{type}/versions

List versions for a specific payload type.

**Response:**
```json
{
  "payload_type": "drone_binary",
  "versions": [...],
  "latest": {
    "version": "0.4.0",
    "hash": "abc123..."
  }
}
```

#### GET /admin/api/payloads/{type}/deploy-log

Deployment history.

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 100 | Maximum records |
| `drone` | string | | Filter by drone |

**Response:**
```json
{
  "payload_type": "drone_binary",
  "history": [
    {
      "id": 1,
      "drone_id": "abc123",
      "drone_name": "drone-01",
      "payload_type": "drone_binary",
      "version": "0.4.0",
      "action": "deploy",
      "status": "success",
      "duration_ms": 2340.5,
      "error_message": null,
      "deployed_at": 1707745234,
      "deployed_by": "admin"
    }
  ]
}
```

#### POST /admin/api/payloads/{type}/{version}/deploy

Deploy to a single drone.

**Request:**
```json
{
  "drone": "drone-01",
  "verify": true,
  "deployed_by": "admin"
}
```

**Response:**
```json
{
  "status": "ok",
  "drone": "drone-01",
  "payload_type": "drone_binary",
  "version": "0.4.0",
  "message": "Deployed drone_binary v0.4.0 to drone-01"
}
```

#### POST /admin/api/payloads/{type}/{version}/rolling-deploy

Rolling deploy to multiple drones.

**Request:**
```json
{
  "drones": ["drone-01", "drone-02"],
  "health_check": true,
  "rollback_on_fail": true,
  "deployed_by": "admin"
}
```

If `drones` is null or omitted, deploys to all outdated drones.

**Response:**
```json
{
  "status": "ok",
  "payload_type": "drone_binary",
  "version": "0.4.0",
  "success_count": 2,
  "fail_count": 0,
  "results": {
    "drone-01": {
      "success": true,
      "message": "Deployed drone_binary v0.4.0 to drone-01"
    },
    "drone-02": {
      "success": true,
      "message": "Deployed drone_binary v0.4.0 to drone-02"
    }
  }
}
```

#### POST /admin/api/payloads/{type}/verify

Verify payload hash on a drone.

**Request:**
```json
{
  "drone": "drone-01"
}
```

**Response:**
```json
{
  "drone": "drone-01",
  "payload_type": "drone_binary",
  "matches": true,
  "remote_hash": "abc123...",
  "message": "Hash matches"
}
```

---

## Drone API (Internal)

These endpoints are used by drones to communicate with the control plane.

### POST /api/v1/register

Drone registration/heartbeat.

**Request:**
```json
{
  "id": "abc123...",
  "name": "drone-01",
  "ip": "10.0.0.175",
  "type": "drone",
  "capabilities": {
    "cores": 16,
    "ram_gb": 32.0,
    "auto_reboot": true,
    "portage_timestamp": "2026-02-12"
  },
  "metrics": {
    "cpu_percent": 45.2,
    "ram_percent": 62.1,
    "load_1m": 2.4
  },
  "current_task": "dev-libs/openssl-3.2.0",
  "version": "0.4.0-alpha"
}
```

**Response:**
```json
{
  "status": "registered",
  "orchestrator": "10.0.0.100",
  "orchestrator_port": 8100,
  "orchestrator_name": "build-swarm-v3",
  "paused": false
}
```

### GET /api/v1/work

Request work assignment.

**Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Drone ID |
| `cores` | int | Available cores |

**Response (has work):**
```json
{
  "package": "dev-libs/openssl-3.2.0",
  "session_id": "session-20260212"
}
```

**Response (no work):**
```json
{
  "package": null
}
```

### POST /api/v1/complete

Report build completion.

**Request:**
```json
{
  "id": "abc123...",
  "package": "dev-libs/openssl-3.2.0",
  "status": "success",
  "build_duration_s": 323.5,
  "error_detail": null
}
```

**Status values:**
- `success` - Build completed successfully
- `failed` - Build failed
- `returned` - Returning package without building

**Response:**
```json
{
  "status": "ok",
  "package": "dev-libs/openssl-3.2.0"
}
```

---

## Error Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 400 | Bad request (invalid input) |
| 401 | Authentication required |
| 404 | Not found |
| 409 | Conflict (e.g., version exists) |
| 410 | Gone (deprecated endpoint) |
| 500 | Internal server error |

---

## Rate Limits

No rate limits are currently enforced. Consider implementing for public-facing deployments.

---

## WebSocket Endpoints

### /ws/ssh/{drone_name}

WebSocket SSH terminal bridge.

**Protocol:**
1. Connect to `ws://host:8093/ws/ssh/drone-01`
2. Receive: `{"type": "connected", "drone": "drone-01", "ip": "10.0.0.175", "user": "root"}`
3. Send input: `{"type": "input", "data": "ls -la\n"}`
4. Receive output: `{"type": "output", "data": "total 123..."}`
5. Send resize: `{"type": "resize", "cols": 80, "rows": 24}`

---

*Gentoo Build Swarm API Reference v0.4.0-alpha*
