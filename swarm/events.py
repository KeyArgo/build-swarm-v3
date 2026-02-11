"""
Event ring buffer for Build Swarm v3 activity feed.

v3.1: Dual-write to in-memory ring buffer (low-latency polling) AND
SQLite (persistence across restarts). Hydrate ring buffer on startup.

Standalone module to avoid circular imports between control_plane, scheduler, health.
"""

import json
import threading
import time

# In-memory ring buffer (max 200 events)
_events = []
_events_lock = threading.Lock()
_event_id = 0

# Database reference (set during init)
_db = None


def init_events(db):
    """Initialize the events module with a database reference.

    Hydrates the in-memory ring buffer from the last 200 events in SQLite,
    so the dashboard has recent history immediately after CP restart.
    """
    global _db, _events, _event_id
    _db = db

    # Ensure events table exists (schema.sql handles this, but be safe)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL DEFAULT (strftime('%s','now')),
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT,
                drone_id TEXT,
                package TEXT
            )
        """)
    except Exception:
        pass

    # Hydrate ring buffer from SQLite
    try:
        rows = db.fetchall("""
            SELECT id, event_type, message, details_json, drone_id, package, timestamp
            FROM events ORDER BY id DESC LIMIT 200
        """)
        with _events_lock:
            _events.clear()
            for row in reversed(rows):
                details = {}
                if row['details_json']:
                    try:
                        details = json.loads(row['details_json'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                _events.append({
                    'id': row['id'],
                    'type': row['event_type'],
                    'message': row['message'],
                    'details': details,
                    'timestamp': row['timestamp'],
                })
            if _events:
                _event_id = _events[-1]['id']
    except Exception:
        pass  # Fresh database, no events to hydrate


def add_event(event_type: str, message: str, details: dict = None):
    """Append an event to the ring buffer AND SQLite for the activity feed.

    Event types: assign, complete, fail, rebalance, grounded, reclaim,
                 register, offline, queue, control, unblock, return, stale
    """
    global _event_id
    now = time.time()
    details = details or {}

    # Extract drone_id and package from details for indexed queries
    drone_id = details.get('drone_id') or details.get('drone')
    package = details.get('package')
    details_json = json.dumps(details) if details else None

    # Write to SQLite first (persistent)
    db_id = None
    if _db is not None:
        try:
            cursor = _db.execute("""
                INSERT INTO events (timestamp, event_type, message, details_json, drone_id, package)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now, event_type, message, details_json, drone_id, package))
            db_id = cursor.lastrowid
        except Exception:
            pass

    # Write to in-memory ring buffer
    with _events_lock:
        if db_id:
            _event_id = db_id
        else:
            _event_id += 1

        _events.append({
            'id': _event_id,
            'type': event_type,
            'message': message,
            'details': details,
            'timestamp': now,
        })
        if len(_events) > 200:
            _events[:] = _events[-200:]


def get_events_since(since_id: int = 0) -> tuple:
    """Get events newer than since_id. Returns (events_list, latest_id)."""
    with _events_lock:
        new = [e for e in _events if e['id'] > since_id]
        return new, _event_id


def get_events_db(since_ts: float = None, event_type: str = None,
                  drone_id: str = None, limit: int = 500) -> list:
    """Query persistent events from SQLite with filters."""
    if _db is None:
        return []

    conditions = []
    params = []

    if since_ts:
        conditions.append("timestamp > ?")
        params.append(since_ts)
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if drone_id:
        conditions.append("drone_id = ?")
        params.append(drone_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(min(limit, 2000))

    rows = _db.fetchall(f"""
        SELECT id, timestamp, event_type, message, details_json, drone_id, package
        FROM events {where}
        ORDER BY id DESC LIMIT ?
    """, tuple(params))

    result = []
    for row in reversed(rows):
        details = {}
        if row['details_json']:
            try:
                details = json.loads(row['details_json'])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append({
            'id': row['id'],
            'type': row['event_type'],
            'message': row['message'],
            'details': details,
            'timestamp': row['timestamp'],
        })
    return result


def prune_old_events(max_age_days: int = 7):
    """Remove events older than max_age_days from SQLite."""
    if _db is None:
        return
    cutoff = time.time() - (max_age_days * 86400)
    _db.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
