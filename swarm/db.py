"""
SQLite database layer for Build Swarm v3.

All state lives here. WAL mode for concurrent reads during writes.
Thread-safe via connection-per-thread pattern.
"""

import json
import sqlite3
import threading
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

log = logging.getLogger('swarm-v3')

SCHEMA_FILE = Path(__file__).resolve().parent / 'schema.sql'
# Fallback: check project root if running from source checkout
if not SCHEMA_FILE.exists():
    SCHEMA_FILE = Path(__file__).resolve().parent.parent / 'schema.sql'
DEFAULT_DB_PATH = '/var/lib/build-swarm-v3/swarm.db'


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
            conn.commit()
        except Exception as e:
            log.debug(f"Migration note: {e}")

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
                       total_packages: int = 0) -> dict:
        """Create a new build session."""
        self.execute("""
            INSERT INTO sessions (id, name, total_packages) VALUES (?, ?, ?)
        """, (session_id, name, total_packages))
        return {'id': session_id, 'name': name, 'status': 'active'}

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

    def queue_packages(self, packages: List[str], session_id: str = None) -> int:
        """Add packages to the queue. Returns count added."""
        added = 0
        for pkg in packages:
            # Skip if already in queue with same session (or any active status)
            existing = self.fetchone("""
                SELECT id FROM queue
                WHERE package = ? AND status IN ('needed', 'delegated')
                AND (session_id = ? OR ? IS NULL)
            """, (pkg, session_id, session_id))
            if existing:
                continue

            self.execute("""
                INSERT INTO queue (package, status, session_id)
                VALUES (?, 'needed', ?)
            """, (pkg, session_id))
            added += 1

        return added

    def get_queue_counts(self, session_id: str = None) -> dict:
        """Get queue status counts."""
        where = "WHERE session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()

        rows = self.fetchall(f"""
            SELECT status, COUNT(*) as cnt FROM queue {where} GROUP BY status
        """, params)

        counts = {'needed': 0, 'delegated': 0, 'received': 0,
                  'blocked': 0, 'failed': 0}
        for row in rows:
            counts[row['status']] = row['cnt']

        counts['total'] = sum(counts.values())
        return counts

    def get_needed_packages(self, limit: int = 50,
                            session_id: str = None) -> List[dict]:
        """Get packages waiting to be built."""
        if session_id:
            rows = self.fetchall("""
                SELECT * FROM queue WHERE status = 'needed' AND session_id = ?
                ORDER BY id LIMIT ?
            """, (session_id, limit))
        else:
            rows = self.fetchall("""
                SELECT * FROM queue WHERE status = 'needed'
                ORDER BY id LIMIT ?
            """, (limit,))
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
            # Mark as received
            self.execute("""
                UPDATE queue SET status = 'received', completed_at = ?,
                    failure_count = 0, error_message = NULL
                WHERE package = ? AND status = 'delegated' AND assigned_to = ?
            """, (now, package, drone_id))

        elif status == 'returned':
            # Re-queue (not a failure)
            self.execute("""
                UPDATE queue SET status = 'needed', assigned_to = NULL, assigned_at = NULL
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

        # Record in build history
        drone_name = self.get_drone_name(drone_id)
        session = self.get_active_session()
        session_id = session['id'] if session else None

        self.execute("""
            INSERT INTO build_history
                (package, drone_id, drone_name, status, duration_seconds,
                 error_message, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (package, drone_id, drone_name, status, duration_seconds,
              error_message, session_id))

        # Update session counts
        if session_id:
            self.update_session_counts(session_id)

        return True

    def reclaim_package(self, package: str) -> bool:
        """Reclaim a delegated package back to needed queue."""
        cursor = self.execute("""
            UPDATE queue SET status = 'needed', assigned_to = NULL, assigned_at = NULL
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
                    assigned_at = NULL, completed_at = NULL,
                    failure_count = 0, error_message = NULL
                WHERE session_id = ? AND status != 'received'
            """, (session_id,))
        else:
            cursor = self.execute("""
                UPDATE queue SET status = 'needed', assigned_to = NULL,
                    assigned_at = NULL, completed_at = NULL,
                    failure_count = 0, error_message = NULL
                WHERE status NOT IN ('received')
            """)
        return cursor.rowcount

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

    # ── Utility ───────────────────────────────────────────────────────

    def close(self):
        """Close the thread-local connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
