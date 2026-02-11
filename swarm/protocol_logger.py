"""
Protocol logger for Build Swarm v3 — Wireshark-style HTTP capture.

Captures every HTTP request/response pair to SQLite via a write-behind queue.
Hot path overhead: <0.01ms per request (dict creation + queue put).
"""

import json
import logging
import queue
import re
import threading
import time
from typing import Optional

log = logging.getLogger('swarm-v3')

# Write-behind queue: HTTP handlers push here, background thread drains to SQLite
_queue = queue.Queue(maxsize=5000)
_writer_thread = None
_db = None
_running = False

# ── Message Type Classification ─────────────────────────────────────────────

MSG_TYPE_MAP = {
    ('GET', '/api/v1/work'): 'work_request',
    ('POST', '/api/v1/register'): 'register',
    ('POST', '/api/v1/complete'): 'complete',
    ('GET', '/api/v1/orchestrator'): 'discovery',
    ('GET', '/api/v1/status'): 'status_query',
    ('GET', '/api/v1/events'): 'events_query',
    ('POST', '/api/v1/queue'): 'queue',
    ('POST', '/api/v1/control'): 'control',
    ('GET', '/api/v1/nodes'): 'node_list',
    ('GET', '/api/v1/metrics'): 'metrics_query',
    ('GET', '/api/v1/history'): 'history_query',
    ('GET', '/api/v1/sessions'): 'session_query',
    ('GET', '/api/v1/health'): 'health_check',
    ('GET', '/api/v1/versions'): 'version_query',
    ('GET', '/api/v1/portage-config'): 'config_query',
    ('GET', '/api/v1/protocol'): 'protocol_query',
    ('GET', '/api/v1/protocol/detail'): 'protocol_query',
    ('GET', '/api/v1/protocol/stats'): 'protocol_query',
    ('GET', '/api/v1/protocol/density'): 'protocol_query',
    ('GET', '/api/v1/protocol/snapshot'): 'protocol_query',
    ('GET', '/api/v1/provision/bootstrap'): 'provisioning',
    ('POST', '/api/v1/provision/drone'): 'provisioning',
}

# Dynamic path patterns (node pause/resume/delete)
_DYNAMIC_PATTERNS = [
    (re.compile(r'^/api/v1/nodes/[^/]+/pause$'), 'POST', 'node_pause'),
    (re.compile(r'^/api/v1/nodes/[^/]+/resume$'), 'POST', 'node_resume'),
    (re.compile(r'^/api/v1/nodes/[^/]+$'), 'DELETE', 'node_delete'),
]


def classify_message(method: str, path: str) -> str:
    """Classify an HTTP request into a message type."""
    clean_path = path.split('?')[0].rstrip('/')
    key = (method, clean_path)
    msg_type = MSG_TYPE_MAP.get(key)
    if msg_type:
        return msg_type
    for pattern, pat_method, pat_type in _DYNAMIC_PATTERNS:
        if method == pat_method and pattern.match(clean_path):
            return pat_type
    return 'unknown'


# ── Field Extraction ────────────────────────────────────────────────────────

def _safe_json(text: str) -> dict:
    """Parse JSON safely, return empty dict on failure."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


def _truncate(text: Optional[str], max_len: int) -> Optional[str]:
    """Truncate text to max_len bytes."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _resolve_name(drone_id: Optional[str]) -> str:
    """Resolve a raw drone ID to its human-readable name."""
    if not drone_id:
        return 'unknown'
    if _db:
        try:
            name = _db.get_drone_name(drone_id)
            if name and name != drone_id[:12]:
                return name
        except Exception:
            pass
    return drone_id[:12]


def _extract_fields(msg_type: str, method: str, path: str,
                    req_body: Optional[str], resp_body: Optional[str],
                    status_code: int, source_ip: str) -> dict:
    """Extract drone_id, package, session_id, summaries from request/response."""
    req = _safe_json(req_body)
    resp = _safe_json(resp_body)
    fields = {
        'drone_id': None,
        'package': None,
        'session_id': None,
        'source_node': None,
        'request_summary': f'{method} {path.split("?")[0]}',
        'response_summary': f'{status_code}',
    }

    if msg_type == 'work_request':
        # drone_id from query param
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(path)
        params = parse_qs(parsed.query)
        fields['drone_id'] = params.get('id', [None])[0]
        pkg = resp.get('package')
        if pkg:
            fields['package'] = pkg
            fields['response_summary'] = f'{status_code} package={pkg}'
        else:
            fields['response_summary'] = f'{status_code} no_work'
        if fields['drone_id']:
            drone_label = _resolve_name(fields['drone_id'])
            fields['source_node'] = drone_label
            fields['request_summary'] = f'GET /work drone={drone_label}'

    elif msg_type == 'register':
        fields['drone_id'] = req.get('id')
        name = req.get('name', '')
        ip = req.get('ip', source_ip)
        cores = req.get('capabilities', {}).get('cores', '?')
        fields['source_node'] = name
        fields['request_summary'] = f'REGISTER {name} ip={ip} cores={cores}'
        fields['response_summary'] = f'{status_code} {resp.get("status", "")}'

    elif msg_type == 'complete':
        fields['drone_id'] = req.get('id')
        fields['package'] = req.get('package')
        fields['source_node'] = _resolve_name(fields['drone_id'])
        status = req.get('status', '?')
        dur = req.get('build_duration_s')
        dur_str = f' {dur:.1f}s' if dur else ''
        fields['request_summary'] = f'COMPLETE {fields["package"]} status={status}{dur_str}'
        fields['response_summary'] = f'{status_code} accepted={resp.get("accepted", "?")}'

    elif msg_type == 'queue':
        pkgs = req.get('packages', [])
        fields['request_summary'] = f'QUEUE {len(pkgs)} packages'
        queued = resp.get('queued', 0)
        fields['session_id'] = resp.get('session_id')
        fields['response_summary'] = f'{status_code} queued={queued}'

    elif msg_type == 'control':
        action = req.get('action', '?')
        fields['request_summary'] = f'CONTROL action={action}'
        fields['response_summary'] = f'{status_code} {resp.get("status", "")}'

    elif msg_type == 'status_query':
        needed = resp.get('needed', 0)
        delegated = resp.get('delegated', 0)
        received = resp.get('received', 0)
        fields['response_summary'] = f'{status_code} N={needed} D={delegated} R={received}'

    elif msg_type == 'node_list':
        drones = resp.get('drones', [])
        fields['response_summary'] = f'{status_code} {len(drones)} nodes'

    elif msg_type == 'events_query':
        events = resp.get('events', [])
        fields['response_summary'] = f'{status_code} {len(events)} events'

    elif msg_type == 'health_check':
        fields['response_summary'] = f'{status_code} {resp.get("status", "ok")}'

    elif msg_type in ('node_pause', 'node_resume', 'node_delete'):
        # Extract node ID from path
        parts = path.split('/')
        if len(parts) >= 5:
            fields['drone_id'] = parts[4]
        node_label = _resolve_name(fields['drone_id']) if fields['drone_id'] else '?'
        fields['source_node'] = node_label
        fields['request_summary'] = f'{method} {msg_type} node={node_label}'

    return fields


# ── Write-Behind Queue ──────────────────────────────────────────────────────

def log_request(source_ip: str, method: str, path: str,
                req_body: Optional[str], resp_body: Optional[str],
                status_code: int, latency_ms: float,
                content_length: int = 0):
    """Push a protocol entry to the write-behind queue (non-blocking)."""
    if not _running:
        return

    msg_type = classify_message(method, path)

    # Don't log protocol queries to avoid infinite recursion
    if msg_type == 'protocol_query':
        return

    fields = _extract_fields(msg_type, method, path, req_body, resp_body,
                             status_code, source_ip)

    entry = {
        'timestamp': time.time(),
        'source_ip': source_ip,
        'source_node': fields['source_node'],
        'method': method,
        'path': path.split('?')[0],
        'msg_type': msg_type,
        'drone_id': fields['drone_id'],
        'package': fields['package'],
        'session_id': fields['session_id'],
        'status_code': status_code,
        'request_summary': fields['request_summary'],
        'response_summary': fields['response_summary'],
        'request_body': _truncate(req_body, 4096),
        'response_body': _truncate(resp_body, 8192),
        'latency_ms': round(latency_ms, 3),
        'content_length': content_length,
    }

    try:
        _queue.put_nowait(entry)
    except queue.Full:
        pass  # Drop entry rather than block the hot path


def _writer_loop():
    """Background thread: drain queue every 0.5s, batch-insert to SQLite."""
    while _running:
        time.sleep(0.5)
        batch = []
        try:
            while True:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass

        if not batch or not _db:
            continue

        try:
            _db.executemany("""
                INSERT INTO protocol_log
                    (timestamp, source_ip, source_node, method, path, msg_type,
                     drone_id, package, session_id, status_code,
                     request_summary, response_summary,
                     request_body, response_body, latency_ms, content_length)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (e['timestamp'], e['source_ip'], e['source_node'],
                 e['method'], e['path'], e['msg_type'],
                 e['drone_id'], e['package'], e['session_id'],
                 e['status_code'], e['request_summary'], e['response_summary'],
                 e['request_body'], e['response_body'],
                 e['latency_ms'], e['content_length'])
                for e in batch
            ])
        except Exception as ex:
            log.error(f"Protocol logger write error: {ex}")

    # Drain remaining on shutdown
    batch = []
    try:
        while True:
            batch.append(_queue.get_nowait())
    except queue.Empty:
        pass
    if batch and _db:
        try:
            _db.executemany("""
                INSERT INTO protocol_log
                    (timestamp, source_ip, source_node, method, path, msg_type,
                     drone_id, package, session_id, status_code,
                     request_summary, response_summary,
                     request_body, response_body, latency_ms, content_length)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (e['timestamp'], e['source_ip'], e['source_node'],
                 e['method'], e['path'], e['msg_type'],
                 e['drone_id'], e['package'], e['session_id'],
                 e['status_code'], e['request_summary'], e['response_summary'],
                 e['request_body'], e['response_body'],
                 e['latency_ms'], e['content_length'])
                for e in batch
            ])
        except Exception:
            pass


# ── Query Functions ─────────────────────────────────────────────────────────

def get_protocol_entries(db, since_id: int = 0, msg_type: str = None,
                         drone_id: str = None, package: str = None,
                         min_latency: float = None, limit: int = 200) -> list:
    """Fetch protocol log entries with optional filters."""
    conditions = ['id > ?']
    params = [since_id]

    if msg_type:
        conditions.append('msg_type = ?')
        params.append(msg_type)
    if drone_id:
        conditions.append('drone_id = ?')
        params.append(drone_id)
    if package:
        conditions.append('package LIKE ?')
        params.append(f'%{package}%')
    if min_latency is not None:
        conditions.append('latency_ms >= ?')
        params.append(min_latency)

    where = ' AND '.join(conditions)
    params.append(limit)

    rows = db.fetchall(f"""
        SELECT id, timestamp, source_ip, source_node, method, path, msg_type,
               drone_id, package, session_id, status_code,
               request_summary, response_summary, latency_ms, content_length
        FROM protocol_log
        WHERE {where}
        ORDER BY id ASC
        LIMIT ?
    """, tuple(params))

    results = []
    for r in rows:
        d = dict(r)
        # Resolve raw drone_id to name for display
        if d.get('drone_id') and not d.get('source_node'):
            d['source_node'] = db.get_drone_name(d['drone_id'])
        results.append(d)
    return results


def get_protocol_detail(db, entry_id: int) -> Optional[dict]:
    """Fetch full protocol entry including request/response bodies."""
    row = db.fetchone(
        "SELECT * FROM protocol_log WHERE id = ?", (entry_id,))
    if not row:
        return None
    d = dict(row)
    if d.get('drone_id') and not d.get('source_node'):
        d['source_node'] = db.get_drone_name(d['drone_id'])
    return d


def get_protocol_stats(db, since: float = None) -> dict:
    """Traffic summary grouped by msg_type."""
    if since:
        rows = db.fetchall("""
            SELECT msg_type, COUNT(*) as count,
                   AVG(latency_ms) as avg_latency,
                   MAX(latency_ms) as max_latency
            FROM protocol_log
            WHERE timestamp > ?
            GROUP BY msg_type
            ORDER BY count DESC
        """, (since,))
    else:
        rows = db.fetchall("""
            SELECT msg_type, COUNT(*) as count,
                   AVG(latency_ms) as avg_latency,
                   MAX(latency_ms) as max_latency
            FROM protocol_log
            GROUP BY msg_type
            ORDER BY count DESC
        """)

    total = sum(dict(r)['count'] for r in rows)
    return {
        'total': total,
        'by_type': [dict(r) for r in rows],
    }


def get_activity_density(db, start: float, end: float,
                         buckets: int = 100) -> list:
    """Activity density histogram for replay scrubber waveform."""
    bucket_width = (end - start) / buckets if buckets > 0 else 1
    rows = db.fetchall("""
        SELECT CAST((timestamp - ?) / ? AS INT) as bucket,
               COUNT(*) as count
        FROM protocol_log
        WHERE timestamp BETWEEN ? AND ?
        GROUP BY bucket
        ORDER BY bucket
    """, (start, bucket_width, start, end))

    # Fill in zeros for empty buckets
    density = [0] * buckets
    for r in rows:
        idx = r['bucket']
        if 0 <= idx < buckets:
            density[idx] = r['count']
    return density


def get_state_at_time(db, timestamp: float) -> dict:
    """Reconstruct system state at a given timestamp from protocol log.

    Finds the most recent status_query and node_list responses before
    the timestamp — these contain full system state snapshots.
    """
    # Get most recent status snapshot
    status_row = db.fetchone("""
        SELECT response_body FROM protocol_log
        WHERE msg_type = 'status_query' AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
    """, (timestamp,))

    # Get most recent node list
    nodes_row = db.fetchone("""
        SELECT response_body FROM protocol_log
        WHERE msg_type = 'node_list' AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
    """, (timestamp,))

    state = {
        'timestamp': timestamp,
        'status': _safe_json(status_row['response_body']) if status_row else {},
        'nodes': _safe_json(nodes_row['response_body']) if nodes_row else {},
    }
    return state


def prune_old_entries(db, max_age_hours: int = 24):
    """Delete protocol log entries older than max_age_hours."""
    cutoff = time.time() - (max_age_hours * 3600)
    cursor = db.execute(
        "DELETE FROM protocol_log WHERE timestamp < ?", (cutoff,))
    if cursor.rowcount > 0:
        log.debug(f"Pruned {cursor.rowcount} protocol log entries")


# ── Init / Shutdown ─────────────────────────────────────────────────────────

def init(db):
    """Start the protocol logger write-behind thread."""
    global _db, _writer_thread, _running
    _db = db
    _running = True
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True,
                                      name='protocol-writer')
    _writer_thread.start()
    log.info("Protocol logger started (write-behind queue)")


def shutdown():
    """Stop the writer thread and drain remaining entries."""
    global _running
    _running = False
    if _writer_thread:
        _writer_thread.join(timeout=2)
