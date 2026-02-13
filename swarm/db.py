"""
SQLite database layer for Build Swarm v3.

All state lives here. WAL mode for concurrent reads during writes.
Thread-safe via connection-per-thread pattern.
"""

import json
import re
import sqlite3
import threading
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# Regex to detect versioned package atoms (e.g., "cat/pkg-1.2.3-r1")
# Matches: category/package_name-version where version starts with a digit
_VERSION_RE = re.compile(
    r'^(?P<cat>[a-zA-Z0-9_+-]+)/(?P<pn>[a-zA-Z0-9_+]+(?:-[a-zA-Z][a-zA-Z0-9_+]*)*)'
    r'-(?P<ver>\d[\d._]*[a-z]?(?:_(?:alpha|beta|pre|rc|p)\d*)*(?:-r\d+)?)$'
)


def normalize_atom(atom: str) -> str:
    """Normalize a Portage package atom for emerge compatibility.

    Versioned atoms like 'cat/pkg-1.0' get '=' prefix: '=cat/pkg-1.0'
    Unversioned atoms like 'cat/pkg' stay as-is.
    Already-prefixed atoms like '=cat/pkg-1.0' are returned unchanged.
    """
    if not atom or atom.startswith(('>=', '<=', '<', '>', '~', '!')):
        return atom  # Don't touch comparison operators

    bare = atom.lstrip('=')
    # Separate slot suffix
    slot = ''
    if ':' in bare:
        bare, slot = bare.split(':', 1)
        slot = ':' + slot

    has_version = bool(_VERSION_RE.match(bare))
    if has_version and not atom.startswith('='):
        return f'={bare}{slot}'
    elif not has_version and atom.startswith('='):
        return f'{bare}{slot}'
    return atom

log = logging.getLogger('swarm-v3')

SCHEMA_FILE = Path(__file__).resolve().parent / 'schema.sql'
# Fallback: check project root if running from source checkout
if not SCHEMA_FILE.exists():
    SCHEMA_FILE = Path(__file__).resolve().parent.parent / 'schema.sql'
DEFAULT_DB_PATH = '/var/lib/build-swarm-v3/swarm.db'

# Packages that can NEVER be removed from a drone's @world.
# Deleting any of these bricks the drone. Protected in the allowlist DB
# and always included in the clean diff even if the admin empties the allowlist.
CRITICAL_PACKAGES = frozenset([
    'sys-apps/portage',
    'sys-devel/gcc',
    'sys-devel/binutils',
    'sys-libs/glibc',
    'dev-lang/python',
    'net-misc/openssh',
    'sys-apps/openrc',
])


class SwarmDB:
    """Thread-safe SQLite database for the build swarm."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Initialize schema
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        """Apply schema from schema.sql, then run migrations."""
        conn = self._get_conn()
        schema = SCHEMA_FILE.read_text()
        conn.executescript(schema)
        conn.commit()
        self._migrate()

    def _migrate(self):
        """Safe schema migrations for v3.1+ (non-destructive on existing DBs)."""
        conn = self._get_conn()

        # Check existing drone_health columns and add new ones if missing
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(drone_health)").fetchall()}
            if 'last_probe_result' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN last_probe_result TEXT")
            if 'last_probe_at' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN last_probe_at REAL")
            if 'upload_failures' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN upload_failures INTEGER DEFAULT 0")
            if 'last_upload_failure' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN last_upload_failure REAL")
            # v4: Self-healing escalation tracking
            if 'escalation_level' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN escalation_level INTEGER DEFAULT 0")
            if 'last_escalation_at' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN last_escalation_at REAL")
            if 'escalation_attempts' not in cols:
                conn.execute("ALTER TABLE drone_health ADD COLUMN escalation_attempts INTEGER DEFAULT 0")
            conn.commit()
        except Exception as e:
            log.debug(f"Migration note: {e}")

        # v4: Add self-healing columns to nodes table
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
            if 'drone_type' not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN drone_type TEXT DEFAULT 'unknown'")
            if 'last_ping_at' not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN last_ping_at REAL")
            if 'last_pong_at' not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN last_pong_at REAL")
            if 'ping_latency_ms' not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN ping_latency_ms REAL")
            conn.commit()
        except Exception as e:
            log.debug(f"Nodes v4 migration note: {e}")

        # Add building_since column to queue table (v3.2 delegation fix)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(queue)").fetchall()}
            if 'building_since' not in cols:
                conn.execute("ALTER TABLE queue ADD COLUMN building_since REAL")
            conn.commit()
        except Exception as e:
            log.debug(f"Queue migration note: {e}")

        # Create releases table if missing (v3.1.1+)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version TEXT UNIQUE NOT NULL,
                    name TEXT,
                    status TEXT DEFAULT 'staging'
                        CHECK(status IN ('staging','active','archived','deleted')),
                    package_count INTEGER DEFAULT 0,
                    size_mb REAL DEFAULT 0,
                    path TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s','now')),
                    promoted_at REAL,
                    archived_at REAL,
                    created_by TEXT,
                    notes TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_releases_version ON releases(version)")
            conn.commit()
        except Exception as e:
            log.debug(f"Releases migration note: {e}")

        # Create drone_allowlist table if missing (v3.2 bloat control)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS drone_allowlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drone_id TEXT,
                    package TEXT NOT NULL,
                    reason TEXT,
                    protected INTEGER DEFAULT 0,
                    added_at REAL DEFAULT (strftime('%s','now')),
                    added_by TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_allowlist_drone ON drone_allowlist(drone_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_allowlist_package ON drone_allowlist(package)")
            conn.commit()

            # Seed global defaults if table is empty
            count = conn.execute("SELECT COUNT(*) FROM drone_allowlist").fetchone()[0]
            if count == 0:
                # Matches drone.spec world_packages + essential infra packages.
                # Critical packages get protected=1 (cannot be deleted via API).
                defaults = [
                    # -- Critical (protected=1) -- from drone.spec world_packages --
                    ('sys-apps/portage',    'Package manager (essential)',          1),
                    ('sys-devel/gcc',       'Compiler toolchain (essential)',       1),
                    ('sys-devel/binutils',  'Linker/assembler (essential)',         1),
                    ('sys-libs/glibc',      'C library (essential)',               1),
                    ('dev-lang/python',     'Python runtime (essential)',           1),
                    ('net-misc/openssh',    'Remote access (essential)',            1),
                    ('sys-apps/openrc',     'Init system (essential)',             1),
                    # -- drone.spec world_packages (not critical but required) --
                    ('net-misc/rsync',      'Portage sync (drone.spec)'),
                    ('app-misc/screen',     'Session persistence (drone.spec)'),
                    ('app-portage/gentoolkit', 'Portage utilities (drone.spec)'),
                    # -- Infrastructure packages --
                    ('dev-vcs/git',         'Version control (portage sync)'),
                    ('app-admin/sudo',      'Privilege escalation'),
                    ('net-misc/dhcpcd',     'Network (DHCP)'),
                    ('sys-kernel/gentoo-kernel-bin', 'Kernel'),
                    ('sys-kernel/linux-firmware', 'Hardware firmware'),
                    ('sys-boot/grub',       'Bootloader'),
                    ('net-misc/curl',       'HTTP client'),
                ]
                for entry in defaults:
                    pkg, reason = entry[0], entry[1]
                    protected = entry[2] if len(entry) > 2 else 0
                    conn.execute(
                        "INSERT INTO drone_allowlist (drone_id, package, reason, protected, added_by) "
                        "VALUES (NULL, ?, ?, ?, 'system')",
                        (pkg, reason, protected))
                conn.commit()
                log.info(f"Seeded {len(defaults)} global allowlist defaults ({sum(1 for e in defaults if len(e) > 2 and e[2])} protected)")
        except Exception as e:
            log.debug(f"Allowlist migration note: {e}")

        # Build profiles table (v3.3 — profile-based builds)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS build_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    profile_type TEXT NOT NULL
                        CHECK(profile_type IN ('distribution', 'user')),
                    world_source TEXT NOT NULL,
                    world_packages TEXT,
                    world_hash TEXT,
                    auto_rebuild INTEGER DEFAULT 0,
                    binhost_ip TEXT,
                    binhost_path TEXT,
                    portage_snapshot_id INTEGER,
                    metadata_json TEXT,
                    created_at REAL DEFAULT (strftime('%s','now')),
                    updated_at REAL DEFAULT (strftime('%s','now')),
                    last_sync_at REAL
                )
            """)
            conn.commit()
        except Exception as e:
            log.debug(f"Build profiles migration note: {e}")

        # Portage snapshots table (v3.3 — portage tree archival)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portage_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    size_bytes INTEGER,
                    trigger TEXT,
                    profile_id TEXT,
                    notes TEXT,
                    created_at REAL DEFAULT (strftime('%s','now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON portage_snapshots(timestamp)")
            conn.commit()
        except Exception as e:
            log.debug(f"Portage snapshots migration note: {e}")

        # Add profile_id to queue, sessions, build_history (v3.3)
        for table in ('queue', 'sessions', 'build_history'):
            try:
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if 'profile_id' not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN profile_id TEXT")
                    conn.commit()
                    log.info(f"Added profile_id column to {table}")
            except Exception as e:
                log.debug(f"{table} profile_id migration note: {e}")

        # Add portage_snapshot_id to sessions (v3.3)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if 'portage_snapshot_id' not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN portage_snapshot_id INTEGER")
                conn.commit()
        except Exception as e:
            log.debug(f"Sessions portage_snapshot_id migration note: {e}")

        # Index for profile-filtered queue lookups
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_profile ON queue(profile_id, status)")
            conn.commit()
        except Exception as e:
            log.debug(f"Queue profile index note: {e}")

        # Migration: add protected column if missing + mark critical entries
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(drone_allowlist)").fetchall()}
            if 'protected' not in cols:
                conn.execute("ALTER TABLE drone_allowlist ADD COLUMN protected INTEGER DEFAULT 0")
                conn.commit()
                log.info("Added protected column to drone_allowlist")
            # Ensure critical packages are always marked protected
            for pkg in CRITICAL_PACKAGES:
                conn.execute(
                    "UPDATE drone_allowlist SET protected = 1 WHERE package = ? AND protected = 0",
                    (pkg,))
            # Ensure critical packages exist in global allowlist
            existing = {r[0] for r in conn.execute(
                "SELECT package FROM drone_allowlist WHERE drone_id IS NULL").fetchall()}
            for pkg in CRITICAL_PACKAGES:
                if pkg not in existing:
                    conn.execute(
                        "INSERT INTO drone_allowlist (drone_id, package, reason, protected, added_by) "
                        "VALUES (NULL, ?, 'Critical system package (auto-added)', 1, 'system')",
                        (pkg,))
                    log.info(f"Auto-added missing critical package to allowlist: {pkg}")
            conn.commit()
        except Exception as e:
            log.debug(f"Protected column migration note: {e}")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL with automatic retry on lock."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower():
                time.sleep(0.1)
                cursor = conn.execute(sql, params)
                conn.commit()
                return cursor
            raise

    def executemany(self, sql: str, params_list: list) -> sqlite3.Cursor:
        """Execute SQL for multiple parameter sets."""
        conn = self._get_conn()
        cursor = conn.executemany(sql, params_list)
        conn.commit()
        return cursor

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Fetch a single row."""
        conn = self._get_conn()
        return conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Fetch all rows."""
        conn = self._get_conn()
        return conn.execute(sql, params).fetchall()

    def fetchval(self, sql: str, params: tuple = ()) -> Any:
        """Fetch a single value."""
        row = self.fetchone(sql, params)
        return row[0] if row else None

    # ── Node Operations ──────────────────────────────────────────────

    def upsert_node(self, node_id: str, name: str, ip: str, node_type: str,
                    cores: int = None, ram_gb: float = None,
                    capabilities: dict = None, metrics: dict = None,
                    current_task: str = None, version: str = None,
                    tailscale_ip: str = None) -> dict:
        """Register or update a node (drone heartbeat)."""
        now = time.time()
        caps_json = json.dumps(capabilities) if capabilities else None
        metrics_json = json.dumps(metrics) if metrics else None

        # If a different node previously claimed this name, remove the stale entry
        existing = self.fetchone(
            "SELECT id FROM nodes WHERE name = ? AND id != ?", (name, node_id))
        if existing:
            self.execute("DELETE FROM nodes WHERE id = ?", (existing['id'],))

        self.execute("""
            INSERT INTO nodes (id, name, ip, tailscale_ip, type, cores, ram_gb,
                             status, last_seen, capabilities_json, metrics_json,
                             current_task, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                ip = excluded.ip,
                tailscale_ip = COALESCE(excluded.tailscale_ip, tailscale_ip),
                type = excluded.type,
                cores = COALESCE(excluded.cores, cores),
                ram_gb = COALESCE(excluded.ram_gb, ram_gb),
                status = 'online',
                last_seen = excluded.last_seen,
                capabilities_json = COALESCE(excluded.capabilities_json, capabilities_json),
                metrics_json = COALESCE(excluded.metrics_json, metrics_json),
                current_task = excluded.current_task,
                version = COALESCE(excluded.version, version)
        """, (node_id, name, ip, tailscale_ip, node_type, cores, ram_gb,
              now, caps_json, metrics_json, current_task, version))

        return {'status': 'registered', 'id': node_id}

    def get_node(self, node_id: str) -> Optional[dict]:
        """Get a single node by ID."""
        row = self.fetchone("SELECT * FROM nodes WHERE id = ?", (node_id,))
        return self._row_to_node(row) if row else None

    def get_node_by_name(self, name: str) -> Optional[dict]:
        """Get a single node by name."""
        row = self.fetchone("SELECT * FROM nodes WHERE name = ?", (name,))
        return self._row_to_node(row) if row else None

    def get_all_nodes(self, include_offline: bool = False,
                      node_type: str = None) -> List[dict]:
        """Get all nodes, optionally filtered."""
        conditions = []
        params = []

        if not include_offline:
            conditions.append("status = 'online'")

        if node_type:
            conditions.append("type = ?")
            params.append(node_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.fetchall(f"SELECT * FROM nodes {where} ORDER BY name", tuple(params))
        return [self._row_to_node(r) for r in rows]

    def get_online_drones(self) -> List[dict]:
        """Get all online drones (including sweepers)."""
        return self.get_all_nodes(include_offline=False)

    def update_node_status(self, timeout_seconds: int = 30,
                           stale_seconds: int = 300):
        """Mark nodes offline based on last_seen. Nodes are never auto-deleted
        so the dashboard always has fleet data available."""
        now = time.time()
        cutoff = now - timeout_seconds

        # Mark offline (but keep in DB for dashboard visibility)
        self.execute(
            "UPDATE nodes SET status = 'offline' WHERE last_seen < ? AND status = 'online'",
            (cutoff,))

    def set_node_paused(self, node_id: str, paused: bool) -> bool:
        """Pause or resume a node."""
        cursor = self.execute(
            "UPDATE nodes SET paused = ? WHERE id = ?",
            (1 if paused else 0, node_id))
        return cursor.rowcount > 0

    def remove_node(self, node_id: str) -> bool:
        """Remove a node."""
        cursor = self.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        return cursor.rowcount > 0

    def get_drone_name(self, drone_id: str) -> str:
        """Get human-readable name for a drone ID."""
        name = self.fetchval("SELECT name FROM nodes WHERE id = ?", (drone_id,))
        return name or drone_id[:12]

    def resolve_drone_id(self, name: str) -> Optional[str]:
        """Resolve a drone name to its ID. Returns None if not found."""
        return self.fetchval("SELECT id FROM nodes WHERE name = ?", (name,))

    def set_drone_type(self, node_id: str, drone_type: str) -> bool:
        """Set the drone type (lxc, qemu, bare-metal, unknown)."""
        cursor = self.execute(
            "UPDATE nodes SET drone_type = ? WHERE id = ?",
            (drone_type, node_id))
        return cursor.rowcount > 0

    def get_drone_type(self, node_id: str) -> str:
        """Get the drone type."""
        return self.fetchval(
            "SELECT drone_type FROM nodes WHERE id = ?", (node_id,)) or 'unknown'

    def update_ping_result(self, node_id: str, latency_ms: float):
        """Update ping/pong results for a node."""
        now = time.time()
        self.execute("""
            UPDATE nodes SET
                last_ping_at = ?,
                last_pong_at = ?,
                ping_latency_ms = ?
            WHERE id = ?
        """, (now, now, latency_ms, node_id))

    def update_escalation_state(self, node_id: str, level: int, attempts: int = None):
        """Update escalation state in drone_health."""
        now = time.time()
        if attempts is not None:
            self.execute("""
                INSERT INTO drone_health (node_id, escalation_level, last_escalation_at, escalation_attempts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    escalation_level = excluded.escalation_level,
                    last_escalation_at = excluded.last_escalation_at,
                    escalation_attempts = excluded.escalation_attempts
            """, (node_id, level, now, attempts))
        else:
            self.execute("""
                INSERT INTO drone_health (node_id, escalation_level, last_escalation_at)
                VALUES (?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    escalation_level = excluded.escalation_level,
                    last_escalation_at = excluded.last_escalation_at
            """, (node_id, level, now))

    def reset_escalation_state(self, node_id: str):
        """Reset escalation state when drone recovers."""
        self.execute("""
            UPDATE drone_health SET
                escalation_level = 0,
                escalation_attempts = 0,
                last_escalation_at = NULL
            WHERE node_id = ?
        """, (node_id,))

    def get_escalation_state(self, node_id: str) -> dict:
        """Get current escalation state for a drone."""
        row = self.fetchone("""
            SELECT escalation_level, last_escalation_at, escalation_attempts
            FROM drone_health WHERE node_id = ?
        """, (node_id,))
        if row:
            return {
                'level': row['escalation_level'] or 0,
                'last_escalation_at': row['last_escalation_at'],
                'attempts': row['escalation_attempts'] or 0,
            }
        return {'level': 0, 'last_escalation_at': None, 'attempts': 0}

    def _row_to_node(self, row: sqlite3.Row) -> dict:
        """Convert a database row to a node dict."""
        d = dict(row)
        d['capabilities'] = json.loads(d.pop('capabilities_json') or '{}')
        d['metrics'] = json.loads(d.pop('metrics_json') or '{}')
        d['online'] = d['status'] == 'online'
        d['paused'] = bool(d.get('paused', 0))
        return d

    # ── Session Operations ───────────────────────────────────────────

    def create_session(self, session_id: str, name: str = None,
                       total_packages: int = 0, profile_id: str = None,
                       portage_snapshot_id: int = None) -> dict:
        """Create a new build session."""
        self.execute("""
            INSERT INTO sessions (id, name, total_packages, profile_id, portage_snapshot_id)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, name, total_packages, profile_id, portage_snapshot_id))
        return {'id': session_id, 'name': name, 'status': 'active',
                'profile_id': profile_id}

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get a session by ID."""
        row = self.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return dict(row) if row else None

    def get_active_session(self) -> Optional[dict]:
        """Get the current active session."""
        row = self.fetchone(
            "SELECT * FROM sessions WHERE status = 'active' ORDER BY started_at DESC LIMIT 1")
        return dict(row) if row else None

    def complete_session(self, session_id: str):
        """Mark a session as completed."""
        self.execute("""
            UPDATE sessions SET status = 'completed', completed_at = ?
            WHERE id = ?
        """, (time.time(), session_id))

    def update_session_counts(self, session_id: str):
        """Update session package counts from queue table."""
        self.execute("""
            UPDATE sessions SET
                completed_packages = (SELECT COUNT(*) FROM queue WHERE session_id = ? AND status = 'received'),
                failed_packages = (SELECT COUNT(*) FROM queue WHERE session_id = ? AND status IN ('blocked', 'failed'))
            WHERE id = ?
        """, (session_id, session_id, session_id))

    # ── Queue Operations ─────────────────────────────────────────────

    def queue_packages(self, packages: List[str], session_id: str = None,
                       profile_id: str = None) -> int:
        """Add packages to the queue. Returns count added."""
        added = 0
        for raw_pkg in packages:
            pkg = normalize_atom(raw_pkg)
            # Skip if already in queue with same session (or any active status)
            existing = self.fetchone("""
                SELECT id FROM queue
                WHERE package = ? AND status IN ('needed', 'delegated')
                AND (session_id = ? OR ? IS NULL)
            """, (pkg, session_id, session_id))
            if existing:
                continue

            self.execute("""
                INSERT INTO queue (package, status, session_id, profile_id)
                VALUES (?, 'needed', ?, ?)
            """, (pkg, session_id, profile_id))
            added += 1

        return added

    def get_queue_counts(self, session_id: str = None,
                         profile_id: str = None) -> dict:
        """Get queue status counts."""
        conditions = []
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if profile_id:
            conditions.append("profile_id = ?")
            params.append(profile_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = self.fetchall(f"""
            SELECT status, COUNT(*) as cnt FROM queue {where} GROUP BY status
        """, tuple(params))

        counts = {'needed': 0, 'delegated': 0, 'received': 0,
                  'blocked': 0, 'failed': 0}
        for row in rows:
            counts[row['status']] = row['cnt']

        counts['total'] = sum(counts.values())
        return counts

    def get_needed_packages(self, limit: int = 50,
                            session_id: str = None,
                            profile_id: str = None) -> List[dict]:
        """Get packages waiting to be built."""
        conditions = ["status = 'needed'"]
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if profile_id:
            conditions.append("profile_id = ?")
            params.append(profile_id)
        where = " AND ".join(conditions)
        params.append(limit)
        rows = self.fetchall(
            f"SELECT * FROM queue WHERE {where} ORDER BY id LIMIT ?",
            tuple(params))
        return [dict(r) for r in rows]

    def get_delegated_packages(self, drone_id: str = None) -> List[dict]:
        """Get packages currently assigned to drones."""
        if drone_id:
            rows = self.fetchall("""
                SELECT * FROM queue WHERE status = 'delegated' AND assigned_to = ?
            """, (drone_id,))
        else:
            rows = self.fetchall(
                "SELECT * FROM queue WHERE status = 'delegated'")
        return [dict(r) for r in rows]

    def get_blocked_packages(self) -> List[dict]:
        """Get blocked packages."""
        rows = self.fetchall("SELECT * FROM queue WHERE status = 'blocked'")
        return [dict(r) for r in rows]

    def assign_package(self, queue_id: int, drone_id: str) -> bool:
        """Assign a specific queue entry to a drone."""
        cursor = self.execute("""
            UPDATE queue SET status = 'delegated', assigned_to = ?, assigned_at = ?
            WHERE id = ? AND status = 'needed'
        """, (drone_id, time.time(), queue_id))
        return cursor.rowcount > 0

    def complete_package(self, package: str, drone_id: str, status: str,
                         duration_seconds: float = 0,
                         error_message: str = None) -> bool:
        """Mark a package as completed (success or failure)."""
        now = time.time()

        if status == 'success':
            # Mark as received — first try the exact match (delegated to this drone)
            cursor = self.execute("""
                UPDATE queue SET status = 'received', completed_at = ?,
                    failure_count = 0, error_message = NULL, building_since = NULL
                WHERE package = ? AND status = 'delegated' AND assigned_to = ?
            """, (now, package, drone_id))

            # v3.2: Fallback for "free work" — package was reclaimed (now 'needed')
            # but the drone still built it successfully. Accept the work.
            if cursor.rowcount == 0:
                self.execute("""
                    UPDATE queue SET status = 'received', completed_at = ?,
                        assigned_to = ?, failure_count = 0, error_message = NULL,
                        building_since = NULL
                    WHERE package = ? AND status = 'needed'
                """, (now, drone_id, package))

        elif status == 'returned':
            # Re-queue (not a failure)
            self.execute("""
                UPDATE queue SET status = 'needed', assigned_to = NULL,
                    assigned_at = NULL, building_since = NULL
                WHERE package = ? AND status = 'delegated' AND assigned_to = ?
            """, (package, drone_id))

        else:
            # Failed — increment failure count, check if should block
            row = self.fetchone("""
                SELECT id, failure_count FROM queue
                WHERE package = ? AND status = 'delegated' AND assigned_to = ?
            """, (package, drone_id))

            if row:
                new_count = row['failure_count'] + 1
                max_failures = 5
                new_status = 'blocked' if new_count >= max_failures else 'needed'
                new_assigned = None if new_status == 'needed' else drone_id

                self.execute("""
                    UPDATE queue SET
                        status = ?,
                        failure_count = ?,
                        error_message = ?,
                        assigned_to = ?,
                        assigned_at = CASE WHEN ? = 'needed' THEN NULL ELSE assigned_at END
                    WHERE id = ?
                """, (new_status, new_count, error_message, new_assigned,
                      new_status, row['id']))

        # Record in build history (propagate profile_id from queue)
        drone_name = self.get_drone_name(drone_id)
        session = self.get_active_session()
        session_id = session['id'] if session else None
        queue_row = self.fetchone(
            "SELECT profile_id FROM queue WHERE package = ? LIMIT 1", (package,))
        hist_profile_id = queue_row['profile_id'] if queue_row else None

        self.execute("""
            INSERT INTO build_history
                (package, drone_id, drone_name, status, duration_seconds,
                 error_message, session_id, profile_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (package, drone_id, drone_name, status, duration_seconds,
              error_message, session_id, hist_profile_id))

        # Update session counts
        if session_id:
            self.update_session_counts(session_id)

        return True

    def reclaim_package(self, package: str) -> bool:
        """Reclaim a delegated package back to needed queue."""
        cursor = self.execute("""
            UPDATE queue SET status = 'needed', assigned_to = NULL,
                assigned_at = NULL, building_since = NULL
            WHERE package = ? AND status = 'delegated'
        """, (package,))
        return cursor.rowcount > 0

    def unblock_package(self, package: str) -> bool:
        """Unblock a single package, reset its failure count."""
        cursor = self.execute("""
            UPDATE queue SET status = 'needed', failure_count = 0,
                error_message = NULL, assigned_to = NULL
            WHERE package = ? AND status IN ('blocked', 'failed')
        """, (package,))
        return cursor.rowcount > 0

    def block_package(self, package: str) -> bool:
        """Block a single package (manual block)."""
        cursor = self.execute("""
            UPDATE queue SET status = 'blocked', error_message = 'manually blocked'
            WHERE package = ? AND status NOT IN ('received', 'blocked')
        """, (package,))
        return cursor.rowcount > 0

    def unblock_all(self) -> int:
        """Unblock all blocked packages, reset failure counts."""
        cursor = self.execute("""
            UPDATE queue SET status = 'needed', failure_count = 0,
                error_message = NULL, assigned_to = NULL
            WHERE status = 'blocked'
        """)
        return cursor.rowcount

    def reset_queue(self, session_id: str = None) -> int:
        """Reset queue — clear all non-needed statuses."""
        if session_id:
            cursor = self.execute("""
                UPDATE queue SET status = 'needed', assigned_to = NULL,
                    assigned_at = NULL, building_since = NULL,
                    completed_at = NULL, failure_count = 0, error_message = NULL
                WHERE session_id = ? AND status != 'received'
            """, (session_id,))
        else:
            cursor = self.execute("""
                UPDATE queue SET status = 'needed', assigned_to = NULL,
                    assigned_at = NULL, building_since = NULL,
                    completed_at = NULL, failure_count = 0, error_message = NULL
                WHERE status NOT IN ('received')
            """)
        return cursor.rowcount

    # ── Build Tracking (v3.2 delegation fix) ─────────────────────────

    def mark_building(self, package: str, drone_id: str) -> bool:
        """Mark a delegated package as actively being built by its assigned drone.

        Called when a drone's heartbeat current_task matches one of its own
        delegated packages. Only sets building_since once (first match wins).
        """
        cursor = self.execute("""
            UPDATE queue SET building_since = ?
            WHERE package = ? AND assigned_to = ? AND status = 'delegated'
              AND building_since IS NULL
        """, (time.time(), package, drone_id))
        return cursor.rowcount > 0

    # ── Assignment Validation (v3.1 stale completion filtering) ──────

    def is_package_assigned_to(self, package: str, drone_id: str) -> bool:
        """Check if a package is currently delegated to this drone."""
        return bool(self.fetchval("""
            SELECT 1 FROM queue
            WHERE package = ? AND assigned_to = ? AND status = 'delegated'
            LIMIT 1
        """, (package, drone_id)))

    def has_drone_failed_package(self, drone_id: str, package: str) -> bool:
        """Check if this drone has previously failed to build this package.

        Only counts actual build failures, NOT upload failures (which are
        infrastructure issues, not package-specific problems).
        """
        return bool(self.fetchval("""
            SELECT 1 FROM build_history
            WHERE drone_id = ? AND package = ?
              AND status NOT IN ('success', 'returned', 'upload_failed')
            LIMIT 1
        """, (drone_id, package)))

    def count_distinct_drone_failures(self, package: str) -> int:
        """Count how many different drones have failed this package.

        Only counts actual build failures, NOT upload failures.
        """
        return self.fetchval("""
            SELECT COUNT(DISTINCT drone_id) FROM build_history
            WHERE package = ? AND status NOT IN ('success', 'returned', 'upload_failed')
        """, (package,)) or 0

    # ── Drone Health (Circuit Breaker) ────────────────────────────────

    def get_drone_health(self, node_id: str) -> dict:
        """Get circuit breaker state for a drone."""
        row = self.fetchone(
            "SELECT * FROM drone_health WHERE node_id = ?", (node_id,))
        if row:
            return dict(row)
        return {'node_id': node_id, 'failures': 0, 'last_failure': None,
                'rebooted': 0, 'grounded_until': None}

    def record_drone_failure(self, node_id: str) -> dict:
        """Record a drone build failure (circuit breaker)."""
        now = time.time()
        self.execute("""
            INSERT INTO drone_health (node_id, failures, last_failure)
            VALUES (?, 1, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                failures = failures + 1,
                last_failure = ?
        """, (node_id, now, now))
        return self.get_drone_health(node_id)

    def record_upload_failure(self, node_id: str):
        """Record a drone upload failure (network-aware scheduling)."""
        now = time.time()
        self.execute("""
            INSERT INTO drone_health (node_id, upload_failures, last_upload_failure)
            VALUES (?, 1, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                upload_failures = upload_failures + 1,
                last_upload_failure = ?
        """, (node_id, now, now))

    def reset_upload_failures(self, node_id: str):
        """Reset upload failure count for a drone (e.g., after successful upload)."""
        self.execute(
            "UPDATE drone_health SET upload_failures = 0 WHERE node_id = ?",
            (node_id,))

    def is_upload_impaired(self, node_id: str, threshold: int, retry_minutes: int) -> bool:
        """Check if a drone has too many consecutive upload failures.

        Returns True if upload_failures >= threshold AND the last failure
        is recent (within retry_minutes). If enough time has passed,
        allows a retry by returning False.
        """
        row = self.fetchone(
            "SELECT upload_failures, last_upload_failure FROM drone_health WHERE node_id = ?",
            (node_id,))
        if not row or (row['upload_failures'] or 0) < threshold:
            return False
        # Allow periodic retry
        last = row['last_upload_failure'] or 0
        if time.time() - last > retry_minutes * 60:
            return False  # Enough time passed, let them try again
        return True

    def reset_drone_health(self, node_id: str = None):
        """Reset circuit breaker for a drone (or all drones)."""
        if node_id:
            self.execute(
                "UPDATE drone_health SET failures = 0, rebooted = 0, grounded_until = NULL WHERE node_id = ?",
                (node_id,))
        else:
            self.execute(
                "UPDATE drone_health SET failures = 0, rebooted = 0, grounded_until = NULL")

    def ground_drone(self, node_id: str, until: float):
        """Ground a drone until a specific time."""
        self.execute("""
            UPDATE drone_health SET grounded_until = ? WHERE node_id = ?
        """, (until, node_id))

    def mark_drone_rebooted(self, node_id: str):
        """Mark that a drone has been rebooted."""
        self.execute(
            "UPDATE drone_health SET rebooted = 1 WHERE node_id = ?",
            (node_id,))

    # ── Config Operations ─────────────────────────────────────────────

    def get_config(self, key: str, default: str = None) -> Optional[str]:
        """Get a config value."""
        val = self.fetchval("SELECT value FROM config WHERE key = ?", (key,))
        return val if val is not None else default

    def set_config(self, key: str, value: str):
        """Set a config value."""
        self.execute("""
            INSERT INTO config (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (key, value, time.time()))

    def get_config_json(self, key: str, default: Any = None) -> Any:
        """Get a config value parsed as JSON."""
        val = self.get_config(key)
        if val is not None:
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                pass
        return default

    def set_config_json(self, key: str, value: Any):
        """Set a config value as JSON."""
        self.set_config(key, json.dumps(value))

    # ── Metrics Logging ───────────────────────────────────────────────

    def log_metrics(self, node_id: str = None, cpu_percent: float = None,
                    ram_percent: float = None, load_1m: float = None):
        """Log a metrics snapshot (for time-series charting)."""
        now = time.time()
        counts = self.get_queue_counts()

        self.execute("""
            INSERT INTO metrics_log
                (timestamp, node_id, cpu_percent, ram_percent, load_1m,
                 queue_needed, queue_delegated, queue_received, queue_blocked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, node_id, cpu_percent, ram_percent, load_1m,
              counts['needed'], counts['delegated'],
              counts['received'], counts['blocked']))

    def get_metrics(self, since: float = None, node_id: str = None,
                    limit: int = 500) -> List[dict]:
        """Get metrics log entries for charting."""
        conditions = []
        params = []

        if since:
            conditions.append("timestamp > ?")
            params.append(since)
        if node_id:
            conditions.append("node_id = ?")
            params.append(node_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self.fetchall(f"""
            SELECT * FROM metrics_log {where}
            ORDER BY timestamp DESC LIMIT ?
        """, tuple(params))

        return [dict(r) for r in reversed(rows)]

    def prune_old_metrics(self, max_age_hours: int = 24):
        """Remove metrics older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        self.execute("DELETE FROM metrics_log WHERE timestamp < ?", (cutoff,))

    # ── Build History ─────────────────────────────────────────────────

    def get_build_history(self, session_id: str = None,
                          limit: int = 100) -> List[dict]:
        """Get build history entries."""
        if session_id:
            rows = self.fetchall("""
                SELECT * FROM build_history
                WHERE session_id = ?
                ORDER BY built_at DESC LIMIT ?
            """, (session_id, limit))
        else:
            rows = self.fetchall("""
                SELECT * FROM build_history
                ORDER BY built_at DESC LIMIT ?
            """, (limit,))
        return [dict(r) for r in rows]

    def get_build_stats(self, session_id: str = None) -> dict:
        """Get aggregate build statistics."""
        where = "WHERE session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()

        total = self.fetchval(
            f"SELECT COUNT(*) FROM build_history {where}", params) or 0
        success = self.fetchval(
            f"SELECT COUNT(*) FROM build_history {where} {'AND' if where else 'WHERE'} status = 'success'",
            params) or 0
        avg_duration = self.fetchval(
            f"SELECT AVG(duration_seconds) FROM build_history {where} {'AND' if where else 'WHERE'} status = 'success' AND duration_seconds > 0",
            params) or 0
        total_duration = self.fetchval(
            f"SELECT SUM(duration_seconds) FROM build_history {where} {'AND' if where else 'WHERE'} status = 'success' AND duration_seconds > 0",
            params) or 0

        # Per-drone breakdown
        and_clause = 'AND' if where else 'WHERE'
        drone_rows = self.fetchall(f"""
            SELECT drone_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   AVG(CASE WHEN status='success' AND duration_seconds > 0
                       THEN duration_seconds END) as avg_duration
            FROM build_history {where}
            GROUP BY drone_name
            ORDER BY total DESC
        """, params)

        per_drone = []
        for dr in drone_rows:
            d = dict(dr)
            d['success_rate'] = round(d['success'] / d['total'] * 100, 1) if d['total'] > 0 else 0
            d['avg_duration'] = round(d['avg_duration'] or 0, 1)
            per_drone.append(d)

        return {
            'total_builds': total,
            'successful': success,
            'failed': total - success,
            'success_rate': round(success / total * 100, 1) if total > 0 else 0,
            'avg_duration_s': round(avg_duration, 1),
            'total_duration_s': round(total_duration, 1),
            'per_drone': per_drone,
        }

    def get_estimated_duration(self, package: str) -> Optional[float]:
        """Estimate build duration for a package from history.

        Tries exact match first, then category average, then global average.
        """
        # Exact package match
        dur = self.fetchval("""
            SELECT AVG(duration_seconds) FROM build_history
            WHERE package = ? AND status = 'success' AND duration_seconds > 0
        """, (package,))
        if dur:
            return round(dur, 1)
        # Category average (e.g. sys-devel/*)
        cat = package.lstrip('=').split('/')[0] if '/' in package else None
        if cat:
            dur = self.fetchval("""
                SELECT AVG(duration_seconds) FROM build_history
                WHERE package LIKE ? AND status = 'success' AND duration_seconds > 0
            """, (f'{cat}/%',))
            if dur:
                return round(dur, 1)
        # Global average
        dur = self.fetchval("""
            SELECT AVG(duration_seconds) FROM build_history
            WHERE status = 'success' AND duration_seconds > 0
        """)
        return round(dur, 1) if dur else None

    def get_metrics_aggregated(self, since: float, bucket_seconds: int = 60) -> List[dict]:
        """Get metrics aggregated into time buckets for charting."""
        rows = self.fetchall("""
            SELECT
                CAST(timestamp / ? AS INT) * ? as bucket,
                AVG(cpu_percent) as avg_cpu,
                AVG(ram_percent) as avg_ram,
                AVG(load_1m) as avg_load,
                MAX(queue_needed) as max_needed,
                MAX(queue_delegated) as max_delegated,
                MAX(queue_received) as max_received,
                MAX(queue_blocked) as max_blocked
            FROM metrics_log
            WHERE timestamp > ? AND node_id IS NULL
            GROUP BY bucket
            ORDER BY bucket
        """, (bucket_seconds, bucket_seconds, since))
        return [dict(r) for r in rows]

    # ── Drone Config (admin-managed per-drone settings) ──────────────

    def get_drone_config(self, node_name: str) -> Optional[dict]:
        """Get admin config for a drone by name."""
        row = self.fetchone("SELECT * FROM drone_config WHERE node_name = ?", (node_name,))
        return dict(row) if row else None

    def get_all_drone_configs(self) -> List[dict]:
        """Get all drone configs."""
        rows = self.fetchall("SELECT * FROM drone_config ORDER BY node_name")
        return [dict(r) for r in rows]

    def upsert_drone_config(self, node_name: str, **fields) -> dict:
        """Create or update drone config. Only updates provided fields."""
        existing = self.get_drone_config(node_name)

        if existing:
            # Update only provided fields
            updates = []
            values = []
            for key, val in fields.items():
                if key in ('node_name', 'created_at'):
                    continue  # Don't update primary key or creation time
                updates.append(f"{key} = ?")
                values.append(val)
            if updates:
                updates.append("updated_at = strftime('%s','now')")
                values.append(node_name)
                sql = f"UPDATE drone_config SET {', '.join(updates)} WHERE node_name = ?"
                self.execute(sql, tuple(values))
        else:
            # Insert new config with defaults
            cols = ['node_name']
            vals = [node_name]
            placeholders = ['?']
            for key, val in fields.items():
                if key in ('node_name', 'created_at', 'updated_at'):
                    continue
                cols.append(key)
                vals.append(val)
                placeholders.append('?')
            sql = f"INSERT INTO drone_config ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
            self.execute(sql, tuple(vals))

        return self.get_drone_config(node_name) or {'node_name': node_name}

    def delete_drone_config(self, node_name: str):
        """Delete drone config."""
        self.execute("DELETE FROM drone_config WHERE node_name = ?", (node_name,))

    def get_ssh_config(self, node_name: str) -> dict:
        """Get SSH connection details for a drone. Falls back to defaults."""
        config = self.get_drone_config(node_name)
        if config:
            return {
                'user': config.get('ssh_user') or 'root',
                'port': config.get('ssh_port') or 22,
                'key_path': config.get('ssh_key_path'),
                'password': config.get('ssh_password'),
            }
        return {'user': 'root', 'port': 22, 'key_path': None, 'password': None}

    # ── Drone Allowlist (v3.2 bloat control) ──────────────────────────

    def get_allowlist(self, drone_name: str = None) -> List[dict]:
        """Get allowlist entries. If drone_name given, includes global + per-drone."""
        if drone_name:
            rows = self.fetchall("""
                SELECT * FROM drone_allowlist
                WHERE drone_id IS NULL OR drone_id = ?
                ORDER BY drone_id NULLS FIRST, package
            """, (drone_name,))
        else:
            rows = self.fetchall(
                "SELECT * FROM drone_allowlist ORDER BY drone_id NULLS FIRST, package")
        return [dict(r) for r in rows]

    def get_allowlist_packages(self, drone_name: str) -> set:
        """Get the set of allowed package names for a drone (global + per-drone)."""
        rows = self.fetchall("""
            SELECT package FROM drone_allowlist
            WHERE drone_id IS NULL OR drone_id = ?
        """, (drone_name,))
        return {r['package'] for r in rows}

    def get_allowlist_with_critical(self, drone_name: str) -> set:
        """Get allowed packages for a drone, guaranteed to include CRITICAL_PACKAGES.

        Even if the admin empties the allowlist, this always returns at least
        the 7 critical system packages that prevent bricking.
        """
        return self.get_allowlist_packages(drone_name) | CRITICAL_PACKAGES

    def add_allowlist(self, package: str, drone_name: str = None,
                      reason: str = None, added_by: str = 'admin') -> int:
        """Add a package to the allowlist. Returns the new row ID."""
        cursor = self.execute("""
            INSERT INTO drone_allowlist (drone_id, package, reason, added_by)
            VALUES (?, ?, ?, ?)
        """, (drone_name, package, reason, added_by))
        return cursor.lastrowid

    def remove_allowlist(self, entry_id: int) -> bool:
        """Remove an allowlist entry by ID. Refuses to delete protected entries.

        Returns True if deleted, False if not found.
        Raises ValueError if the entry is protected.
        """
        row = self.fetchone("SELECT package, protected FROM drone_allowlist WHERE id = ?", (entry_id,))
        if not row:
            return False
        if row['protected']:
            raise ValueError(f"Cannot delete protected package '{row['package']}' — it is critical for drone operation")
        cursor = self.execute(
            "DELETE FROM drone_allowlist WHERE id = ? AND protected = 0", (entry_id,))
        return cursor.rowcount > 0

    def remove_allowlist_by_package(self, package: str, drone_name: str = None) -> bool:
        """Remove an allowlist entry by package name and optional drone.

        Refuses to delete protected entries. Raises ValueError if protected.
        """
        # Check if this is a protected entry
        if drone_name:
            row = self.fetchone(
                "SELECT protected FROM drone_allowlist WHERE package = ? AND drone_id = ?",
                (package, drone_name))
        else:
            row = self.fetchone(
                "SELECT protected FROM drone_allowlist WHERE package = ? AND drone_id IS NULL",
                (package,))
        if row and row['protected']:
            raise ValueError(f"Cannot delete protected package '{package}' — it is critical for drone operation")

        if drone_name:
            cursor = self.execute(
                "DELETE FROM drone_allowlist WHERE package = ? AND drone_id = ? AND protected = 0",
                (package, drone_name))
        else:
            cursor = self.execute(
                "DELETE FROM drone_allowlist WHERE package = ? AND drone_id IS NULL AND protected = 0",
                (package,))
        return cursor.rowcount > 0

    # ── Build Profiles ───────────────────────────────────────────────

    def create_profile(self, profile_id: str, name: str, profile_type: str,
                       world_source: str, auto_rebuild: bool = False,
                       binhost_ip: str = None, binhost_path: str = None,
                       metadata: dict = None) -> dict:
        """Create a new build profile."""
        self.execute("""
            INSERT INTO build_profiles (id, name, profile_type, world_source,
                auto_rebuild, binhost_ip, binhost_path, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (profile_id, name, profile_type, world_source,
              1 if auto_rebuild else 0, binhost_ip, binhost_path,
              json.dumps(metadata) if metadata else None))
        return self.get_profile(profile_id)

    def get_profile(self, profile_id: str) -> Optional[dict]:
        """Get a build profile by ID."""
        row = self.fetchone("SELECT * FROM build_profiles WHERE id = ?",
                            (profile_id,))
        if not row:
            return None
        d = dict(row)
        d['metadata'] = json.loads(d.pop('metadata_json') or '{}')
        d['auto_rebuild'] = bool(d['auto_rebuild'])
        return d

    def get_all_profiles(self) -> List[dict]:
        """Get all build profiles."""
        rows = self.fetchall("SELECT * FROM build_profiles ORDER BY name")
        result = []
        for row in rows:
            d = dict(row)
            d['metadata'] = json.loads(d.pop('metadata_json') or '{}')
            d['auto_rebuild'] = bool(d['auto_rebuild'])
            result.append(d)
        return result

    def update_profile(self, profile_id: str, **fields) -> Optional[dict]:
        """Update specific fields on a profile."""
        allowed = {'name', 'profile_type', 'world_source', 'auto_rebuild',
                   'binhost_ip', 'binhost_path', 'portage_snapshot_id'}
        updates = []
        values = []
        for key, val in fields.items():
            if key in allowed:
                if key == 'auto_rebuild':
                    val = 1 if val else 0
                updates.append(f"{key} = ?")
                values.append(val)
        if not updates:
            return self.get_profile(profile_id)
        updates.append("updated_at = ?")
        values.append(time.time())
        values.append(profile_id)
        self.execute(
            f"UPDATE build_profiles SET {', '.join(updates)} WHERE id = ?",
            tuple(values))
        return self.get_profile(profile_id)

    def update_profile_world(self, profile_id: str, packages: List[str]) -> dict:
        """Update the resolved world packages for a profile."""
        import hashlib
        sorted_pkgs = sorted(set(packages))
        world_text = '\n'.join(sorted_pkgs)
        world_hash = hashlib.sha256(world_text.encode()).hexdigest()[:16]

        self.execute("""
            UPDATE build_profiles
            SET world_packages = ?, world_hash = ?, updated_at = ?
            WHERE id = ?
        """, (world_text, world_hash, time.time(), profile_id))
        return {'package_count': len(sorted_pkgs), 'world_hash': world_hash}

    def get_profile_packages(self, profile_id: str) -> List[str]:
        """Get the resolved package list for a profile."""
        text = self.fetchval(
            "SELECT world_packages FROM build_profiles WHERE id = ?",
            (profile_id,))
        if not text:
            return []
        return [p for p in text.strip().split('\n') if p]

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a build profile."""
        cursor = self.execute("DELETE FROM build_profiles WHERE id = ?",
                              (profile_id,))
        return cursor.rowcount > 0

    # ── Portage Snapshots ────────────────────────────────────────────

    def record_snapshot(self, filename: str, timestamp: str,
                        size_bytes: int = None, trigger: str = None,
                        profile_id: str = None, notes: str = None) -> int:
        """Record a portage tree snapshot in the database. Returns row ID."""
        cursor = self.execute("""
            INSERT INTO portage_snapshots
                (filename, timestamp, size_bytes, trigger, profile_id, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (filename, timestamp, size_bytes, trigger, profile_id, notes))
        return cursor.lastrowid

    def get_snapshots(self, limit: int = 50) -> List[dict]:
        """Get portage snapshots, most recent first."""
        rows = self.fetchall("""
            SELECT * FROM portage_snapshots ORDER BY created_at DESC LIMIT ?
        """, (limit,))
        return [dict(r) for r in rows]

    def get_latest_snapshot(self) -> Optional[dict]:
        """Get the most recent portage snapshot."""
        row = self.fetchone(
            "SELECT * FROM portage_snapshots ORDER BY created_at DESC LIMIT 1")
        return dict(row) if row else None

    def get_snapshot(self, snapshot_id: int) -> Optional[dict]:
        """Get a snapshot by ID."""
        row = self.fetchone(
            "SELECT * FROM portage_snapshots WHERE id = ?", (snapshot_id,))
        return dict(row) if row else None

    # ── Payload Versioning (v4) ─────────────────────────────────────────

    def create_payload_version(self, payload_type: str, version: str, hash: str,
                               content_path: str = None, content_blob: bytes = None,
                               description: str = None, notes: str = None,
                               created_by: str = None) -> int:
        """Create a new payload version. Returns row ID."""
        cursor = self.execute("""
            INSERT INTO payload_versions
                (payload_type, version, hash, content_path, content_blob,
                 description, notes, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (payload_type, version, hash, content_path, content_blob,
              description, notes, created_by))
        return cursor.lastrowid

    def get_payload_version(self, payload_type: str, version: str) -> Optional[dict]:
        """Get a specific payload version."""
        row = self.fetchone("""
            SELECT id, payload_type, version, hash, content_path, description, notes,
                   created_at, created_by
            FROM payload_versions
            WHERE payload_type = ? AND version = ?
        """, (payload_type, version))
        return dict(row) if row else None

    def get_payload_versions(self, payload_type: str = None, limit: int = 50) -> List[dict]:
        """Get payload versions, optionally filtered by type."""
        if payload_type:
            rows = self.fetchall("""
                SELECT id, payload_type, version, hash, content_path, description, notes,
                       created_at, created_by
                FROM payload_versions WHERE payload_type = ?
                ORDER BY created_at DESC LIMIT ?
            """, (payload_type, limit))
        else:
            rows = self.fetchall("""
                SELECT id, payload_type, version, hash, content_path, description, notes,
                       created_at, created_by
                FROM payload_versions ORDER BY created_at DESC LIMIT ?
            """, (limit,))
        return [dict(r) for r in rows]

    def get_latest_payload_version(self, payload_type: str) -> Optional[dict]:
        """Get the most recent version of a payload type."""
        row = self.fetchone("""
            SELECT id, payload_type, version, hash, content_path, description, notes,
                   created_at, created_by
            FROM payload_versions
            WHERE payload_type = ? ORDER BY created_at DESC LIMIT 1
        """, (payload_type,))
        return dict(row) if row else None

    def set_drone_payload(self, drone_id: str, payload_type: str, version: str,
                          hash: str, status: str = 'deployed',
                          deployed_by: str = None) -> bool:
        """Set or update the payload version for a drone."""
        now = time.time()
        self.execute("""
            INSERT INTO drone_payloads
                (drone_id, payload_type, version, hash, status, deployed_at, deployed_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(drone_id, payload_type) DO UPDATE SET
                version = excluded.version,
                hash = excluded.hash,
                status = excluded.status,
                deployed_at = excluded.deployed_at,
                deployed_by = excluded.deployed_by,
                error_message = NULL
        """, (drone_id, payload_type, version, hash, status, now, deployed_by))
        return True

    def get_drone_payload(self, drone_id: str, payload_type: str) -> Optional[dict]:
        """Get a specific payload for a drone."""
        row = self.fetchone("""
            SELECT * FROM drone_payloads WHERE drone_id = ? AND payload_type = ?
        """, (drone_id, payload_type))
        return dict(row) if row else None

    def get_drone_payloads(self, drone_id: str) -> List[dict]:
        """Get all payloads for a drone."""
        rows = self.fetchall("""
            SELECT * FROM drone_payloads WHERE drone_id = ? ORDER BY payload_type
        """, (drone_id,))
        return [dict(r) for r in rows]

    def get_all_drone_payloads(self, payload_type: str = None) -> Dict[str, dict]:
        """Get payload versions for all drones (useful for version matrix)."""
        if payload_type:
            rows = self.fetchall("""
                SELECT dp.drone_id, dp.payload_type, dp.version, dp.hash, dp.status,
                       dp.deployed_at, n.name as drone_name
                FROM drone_payloads dp
                JOIN nodes n ON n.id = dp.drone_id
                WHERE dp.payload_type = ?
            """, (payload_type,))
        else:
            rows = self.fetchall("""
                SELECT dp.drone_id, dp.payload_type, dp.version, dp.hash, dp.status,
                       dp.deployed_at, n.name as drone_name
                FROM drone_payloads dp
                JOIN nodes n ON n.id = dp.drone_id
            """)
        result = {}
        for row in rows:
            d = dict(row)
            drone_name = d.pop('drone_name', d['drone_id'])
            if drone_name not in result:
                result[drone_name] = {}
            result[drone_name][d['payload_type']] = d
        return result

    def log_payload_deploy(self, drone_id: str, payload_type: str, version: str,
                           action: str, status: str, duration_ms: float = None,
                           error_message: str = None, deployed_by: str = None) -> int:
        """Log a payload deployment attempt. Returns row ID."""
        cursor = self.execute("""
            INSERT INTO payload_deploy_log
                (drone_id, payload_type, version, action, status, duration_ms,
                 error_message, deployed_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (drone_id, payload_type, version, action, status, duration_ms,
              error_message, deployed_by))
        return cursor.lastrowid

    def get_payload_deploy_history(self, drone_id: str = None, limit: int = 100) -> List[dict]:
        """Get payload deployment history, optionally filtered by drone."""
        if drone_id:
            rows = self.fetchall("""
                SELECT pdl.*, n.name as drone_name
                FROM payload_deploy_log pdl
                LEFT JOIN nodes n ON n.id = pdl.drone_id
                WHERE pdl.drone_id = ?
                ORDER BY pdl.deployed_at DESC LIMIT ?
            """, (drone_id, limit))
        else:
            rows = self.fetchall("""
                SELECT pdl.*, n.name as drone_name
                FROM payload_deploy_log pdl
                LEFT JOIN nodes n ON n.id = pdl.drone_id
                ORDER BY pdl.deployed_at DESC LIMIT ?
            """, (limit,))
        return [dict(r) for r in rows]

    def get_outdated_drones(self, payload_type: str) -> List[dict]:
        """Get drones that don't have the latest version of a payload."""
        latest = self.get_latest_payload_version(payload_type)
        if not latest:
            return []
        rows = self.fetchall("""
            SELECT n.id as drone_id, n.name, n.ip, n.status,
                   dp.version as current_version, dp.hash as current_hash,
                   dp.deployed_at
            FROM nodes n
            LEFT JOIN drone_payloads dp ON dp.drone_id = n.id AND dp.payload_type = ?
            WHERE n.type = 'drone'
              AND (dp.version IS NULL OR dp.version != ? OR dp.hash != ?)
        """, (payload_type, latest['version'], latest['hash']))
        return [{
            **dict(r),
            'latest_version': latest['version'],
            'latest_hash': latest['hash']
        } for r in rows]

    # ── Utility ───────────────────────────────────────────────────────

    def close(self):
        """Close the thread-local connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
