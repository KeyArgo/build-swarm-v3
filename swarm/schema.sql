-- Build Swarm v3 - SQLite Schema
-- Unified control plane: replaces registry.json + state.json + fleet.json

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Nodes: replaces registry.json (gateway) + drone_status (orchestrator)
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    ip TEXT,
    tailscale_ip TEXT,
    type TEXT CHECK(type IN ('drone','sweeper')) NOT NULL,
    cores INTEGER,
    ram_gb REAL,
    status TEXT DEFAULT 'offline',
    paused INTEGER DEFAULT 0,
    last_seen REAL,                 -- unix timestamp for fast comparison
    capabilities_json TEXT,         -- {arch, auto_reboot, portage_timestamp, ...}
    metrics_json TEXT,              -- {cpu_percent, ram_percent, load_1m, ...}
    current_task TEXT,
    version TEXT,
    created_at REAL DEFAULT (strftime('%s','now'))
);

-- Queue: replaces in-memory needed/delegated/received/failed lists
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package TEXT NOT NULL,
    status TEXT DEFAULT 'needed'
        CHECK(status IN ('needed','delegated','received','blocked','failed')),
    assigned_to TEXT REFERENCES nodes(id),
    assigned_at REAL,
    completed_at REAL,
    failure_count INTEGER DEFAULT 0,
    error_message TEXT,
    session_id TEXT REFERENCES sessions(id),
    created_at REAL DEFAULT (strftime('%s','now'))
);

-- Build history: every build attempt (for analytics)
CREATE TABLE IF NOT EXISTS build_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package TEXT NOT NULL,
    drone_id TEXT,
    drone_name TEXT,
    status TEXT NOT NULL,
    duration_seconds REAL,
    error_message TEXT,
    session_id TEXT,
    built_at REAL DEFAULT (strftime('%s','now'))
);

-- Sessions: groups of packages from a single build run
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active','completed','aborted')),
    total_packages INTEGER DEFAULT 0,
    completed_packages INTEGER DEFAULT 0,
    failed_packages INTEGER DEFAULT 0,
    started_at REAL DEFAULT (strftime('%s','now')),
    completed_at REAL
);

-- Config: key-value store for centralized configuration
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL DEFAULT (strftime('%s','now'))
);

-- Drone health: circuit breaker state (separate from nodes for clarity)
CREATE TABLE IF NOT EXISTS drone_health (
    node_id TEXT PRIMARY KEY REFERENCES nodes(id),
    failures INTEGER DEFAULT 0,
    last_failure REAL,
    rebooted INTEGER DEFAULT 0,
    grounded_until REAL,
    upload_failures INTEGER DEFAULT 0,
    last_upload_failure REAL
);

-- Metrics time-series (ring buffer for charting)
CREATE TABLE IF NOT EXISTS metrics_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    node_id TEXT,
    cpu_percent REAL,
    ram_percent REAL,
    load_1m REAL,
    queue_needed INTEGER,
    queue_delegated INTEGER,
    queue_received INTEGER,
    queue_blocked INTEGER
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_queue_session ON queue(session_id);
CREATE INDEX IF NOT EXISTS idx_queue_assigned ON queue(assigned_to);
CREATE INDEX IF NOT EXISTS idx_queue_package ON queue(package);
CREATE INDEX IF NOT EXISTS idx_history_session ON build_history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_drone ON build_history(drone_id);
CREATE INDEX IF NOT EXISTS idx_history_built ON build_history(built_at);
CREATE INDEX IF NOT EXISTS idx_metrics_time ON metrics_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_node ON metrics_log(node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);

-- Drone admin config: persistent per-drone settings managed from the admin dashboard.
-- Separate from `nodes` (which is self-reported via heartbeat). This is what the
-- admin WANTS, not what the drone reports.
CREATE TABLE IF NOT EXISTS drone_config (
    node_name TEXT PRIMARY KEY,               -- matches nodes.name (e.g. "drone-io")

    -- SSH access
    ssh_user TEXT DEFAULT 'root',
    ssh_port INTEGER DEFAULT 22,
    ssh_key_path TEXT,                        -- e.g. /root/.ssh/id_ed25519 (control plane side)
    ssh_password TEXT,                        -- optional fallback (stored in plaintext — LAN only)

    -- Resource limits (what the drone should use, not what it has)
    cores_limit INTEGER,                      -- MAKEOPTS -j value (NULL = use all)
    emerge_jobs INTEGER DEFAULT 2,            -- EMERGE_DEFAULT_OPTS --jobs=N
    ram_limit_gb REAL,                        -- soft limit for awareness (NULL = no limit)

    -- Build behavior
    auto_reboot INTEGER DEFAULT 1,            -- allow health monitor to reboot this drone
    protected INTEGER DEFAULT 0,              -- prevent accidental deletion/reboot
    max_failures INTEGER,                     -- per-drone circuit breaker threshold (NULL = use global)
    binhost_upload_url TEXT,                  -- where to send built packages (NULL = default)

    -- Identity
    display_name TEXT,                        -- human-friendly name (NULL = use node_name)
    v2_name TEXT,                             -- legacy v2 name mapping (e.g. "drone-Izar")
    control_plane TEXT DEFAULT 'v3'           -- which CP this drone should talk to: v2 | v3
        CHECK(control_plane IN ('v2','v3')),

    -- Bloat protection
    locked INTEGER DEFAULT 1,                 -- should this drone be locked (package.mask + chattr)

    -- Notes
    notes TEXT,                               -- free text for admin notes

    -- Metadata
    updated_at REAL DEFAULT (strftime('%s','now')),
    created_at REAL DEFAULT (strftime('%s','now'))
);

-- Events: persistent activity feed (v3.1)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT,
    details_json TEXT,
    drone_id TEXT,
    package TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

-- Protocol log: every HTTP request/response pair (Wireshark-style capture)
CREATE TABLE IF NOT EXISTS protocol_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source_ip TEXT,
    source_node TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    drone_id TEXT,
    package TEXT,
    session_id TEXT,
    status_code INTEGER,
    request_summary TEXT,
    response_summary TEXT,
    request_body TEXT,
    response_body TEXT,
    latency_ms REAL,
    content_length INTEGER
);
CREATE INDEX IF NOT EXISTS idx_protocol_timestamp ON protocol_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_protocol_type ON protocol_log(msg_type);
CREATE INDEX IF NOT EXISTS idx_protocol_drone ON protocol_log(drone_id);
CREATE INDEX IF NOT EXISTS idx_protocol_package ON protocol_log(package);

-- Releases: versioned snapshots of binary packages
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT UNIQUE NOT NULL,           -- e.g. '2026.02.11' or 'pre-kde6'
    name TEXT,                              -- optional friendly label
    status TEXT DEFAULT 'staging'
        CHECK(status IN ('staging','active','archived','deleted')),
    package_count INTEGER DEFAULT 0,
    size_mb REAL DEFAULT 0,
    path TEXT NOT NULL,                     -- absolute path to release directory
    created_at REAL DEFAULT (strftime('%s','now')),
    promoted_at REAL,
    archived_at REAL,
    created_by TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status);
CREATE INDEX IF NOT EXISTS idx_releases_version ON releases(version);
