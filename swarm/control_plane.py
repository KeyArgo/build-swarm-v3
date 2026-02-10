"""
Build Swarm v3 - Unified Control Plane

Merges gateway + orchestrator into a single HTTP server backed by SQLite.
Runs on port 8100 alongside the existing v2 system.
"""

import json
import logging
import os
import socket
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse, parse_qs

from . import __version__
from . import config as cfg
from . import protocol_logger
from .db import SwarmDB
from .events import add_event, get_events_since
from .health import DroneHealthMonitor
from .scheduler import Scheduler

log = logging.getLogger('swarm-v3')

# Globals initialized in start()
db: SwarmDB = None
health_monitor: DroneHealthMonitor = None
scheduler: Scheduler = None


def get_self_ip() -> str:
    """Get this machine's primary IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.0.0.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class V3Handler(BaseHTTPRequestHandler):
    """Unified HTTP handler for all v3 API endpoints."""

    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        log.debug(f"{self.address_string()} - {format % args}")

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_cors_headers()
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        # Capture for protocol logger
        self._proto_resp_body = body.decode(errors='replace')
        self._proto_status = status
        self._proto_content_length = len(body)

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            return {}
        if length > 1_048_576:
            return {}
        body = self.rfile.read(length).decode()
        # Capture for protocol logger
        self._proto_req_body = body
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    # ── GET Endpoints ─────────────────────────────────────────────

    def _init_proto(self):
        """Initialize protocol capture fields."""
        self._proto_start = time.time()
        self._proto_req_body = None
        self._proto_resp_body = None
        self._proto_status = None
        self._proto_content_length = 0

    def _log_protocol(self):
        """Push captured request/response to protocol logger."""
        try:
            latency = (time.time() - self._proto_start) * 1000
            protocol_logger.log_request(
                source_ip=self.client_address[0],
                method=self.command,
                path=self.path,
                req_body=self._proto_req_body,
                resp_body=self._proto_resp_body,
                status_code=self._proto_status or 0,
                latency_ms=latency,
                content_length=self._proto_content_length,
            )
        except Exception:
            pass

    def do_GET(self):
        self._init_proto()
        try:
            self._handle_get()
        except Exception as e:
            log.error(f"GET {self.path} error: {e}", exc_info=True)
            try:
                self.send_json({'error': 'Internal server error'}, 500)
            except Exception:
                pass
        finally:
            self._log_protocol()

    def _handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = parse_qs(parsed.query)

        # ── Health Check ──
        if path == '/api/v1/health':
            self.send_json({
                'status': 'ok',
                'version': __version__,
                'uptime_s': round(time.time() - _start_time, 1)
            })

        # ── Node Listing (was Gateway /nodes) ──
        elif path == '/api/v1/nodes':
            db.update_node_status(cfg.NODE_TIMEOUT, cfg.STALE_TIMEOUT)
            include_all = params.get('all', ['false'])[0].lower() == 'true'
            nodes = db.get_all_nodes(include_offline=include_all)
            drones = [n for n in nodes if n['type'] in ('drone', 'sweeper')]
            self.send_json({'drones': drones, 'orchestrators': []})

        # ── Orchestrator Discovery (compatibility) ──
        elif path == '/api/v1/orchestrator':
            self_ip = os.environ.get('REPORT_IP', get_self_ip())
            self.send_json({
                'ip': self_ip,
                'name': 'build-swarm-v3',
                'port': cfg.CONTROL_PLANE_PORT
            })

        # ── Work Request (was Orchestrator /work) ──
        elif path == '/api/v1/work':
            drone_id = params.get('id', ['unknown'])[0]
            result = scheduler.get_work(drone_id, self.client_address[0])

            if isinstance(result, dict) and result.get('action'):
                self.send_json(result)
            elif result:
                self.send_json({'package': result})
            else:
                self.send_json({'package': None})

        # ── Queue Status ──
        elif path == '/api/v1/status':
            counts = db.get_queue_counts()
            session = db.get_active_session()
            drones = db.get_all_nodes(include_offline=True)

            # Build drone status map
            drone_status = {}
            drone_health_map = {}
            for d in drones:
                drone_status[d['id']] = {
                    'name': d['name'],
                    'ip': d['ip'],
                    'status': d['status'],
                    'current_task': d.get('current_task'),
                    'capabilities': d.get('capabilities', {}),
                    'metrics': d.get('metrics', {}),
                    'last_seen': d.get('last_seen'),
                }
                h = db.get_drone_health(d['id'])
                if h['failures'] > 0:
                    drone_health_map[d['id']] = h

            # Build package lists for compatibility
            needed_pkgs = [p['package'] for p in db.get_needed_packages(limit=10)]
            delegated_pkgs = {}
            for p in db.get_delegated_packages():
                delegated_pkgs[p['package']] = {
                    'drone': p['assigned_to'],
                    'assigned_at': p.get('assigned_at')
                }
            blocked_pkgs = [p['package'] for p in db.get_blocked_packages()]

            # Timing metrics
            stats = db.get_build_stats(session['id'] if session else None)

            paused = db.get_config('paused', 'false') == 'true'

            self.send_json({
                'needed': counts['needed'],
                'delegated': counts['delegated'],
                'received': counts['received'],
                'blocked': counts['blocked'],
                'failed': counts['failed'],
                'total': counts['total'],
                'paused': paused,
                'session': session,
                'packages': {
                    'needed': needed_pkgs,
                    'delegated': delegated_pkgs,
                    'blocked': blocked_pkgs,
                },
                'drones': drone_status,
                'drone_health': drone_health_map,
                'timing': stats,
                'version': __version__,
            })

        # ── Portage Config (was Gateway /portage-config) ──
        elif path == '/api/v1/portage-config':
            portage_cfg = cfg.get_portage_config()
            self.send_json(portage_cfg)

        # ── Versions ──
        elif path == '/api/v1/versions':
            drones = db.get_all_nodes(include_offline=True)
            self.send_json({
                'control_plane': {
                    'name': 'build-swarm-v3',
                    'version': __version__,
                    'status': 'online'
                },
                'drones': [{
                    'name': d['name'],
                    'ip': d['ip'],
                    'version': d.get('version', 'unknown'),
                    'online': d['online'],
                } for d in drones]
            })

        # ── Metrics (for charting) ──
        elif path == '/api/v1/metrics':
            since = params.get('since', [None])[0]
            since_ts = float(since) if since else time.time() - 3600
            node_id = params.get('node', [None])[0]

            metrics = db.get_metrics(since=since_ts, node_id=node_id)
            counts = db.get_queue_counts()

            # Also get per-drone current metrics
            drones = db.get_all_nodes(include_offline=False)
            drone_metrics = {}
            for d in drones:
                m = d.get('metrics', {})
                if m:
                    drone_metrics[d['name']] = m

            self.send_json({
                'timestamp': time.time(),
                'history': metrics,
                'current_queue': counts,
                'drones': drone_metrics,
            })

        # ── Build History ──
        elif path == '/api/v1/history':
            session_id = params.get('session', [None])[0]
            limit = int(params.get('limit', ['100'])[0])
            history = db.get_build_history(session_id=session_id, limit=limit)
            stats = db.get_build_stats(session_id=session_id)
            self.send_json({'history': history, 'stats': stats})

        # ── Sessions ──
        elif path == '/api/v1/sessions':
            rows = db.fetchall(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 50")
            self.send_json({'sessions': [dict(r) for r in rows]})

        # ── Events (activity feed) ──
        elif path == '/api/v1/events':
            since_id = int(params.get('since', ['0'])[0])
            events, latest_id = get_events_since(since_id)
            self.send_json({'events': events, 'latest_id': latest_id})

        # ── Protocol Log (Wireshark-style) ──
        elif path == '/api/v1/protocol':
            since_id = int(params.get('since', ['0'])[0])
            msg_type = params.get('type', [None])[0]
            drone_id = params.get('drone', [None])[0]
            package = params.get('package', [None])[0]
            min_latency = params.get('min_latency', [None])[0]
            limit = int(params.get('limit', ['200'])[0])
            entries = protocol_logger.get_protocol_entries(
                db, since_id=since_id, msg_type=msg_type,
                drone_id=drone_id, package=package,
                min_latency=float(min_latency) if min_latency else None,
                limit=min(limit, 2000))
            self.send_json({'entries': entries})

        elif path == '/api/v1/protocol/detail':
            entry_id = int(params.get('id', ['0'])[0])
            entry = protocol_logger.get_protocol_detail(db, entry_id)
            if entry:
                self.send_json(entry)
            else:
                self.send_json({'error': 'Entry not found'}, 404)

        elif path == '/api/v1/protocol/stats':
            since = params.get('since', [None])[0]
            since_ts = float(since) if since else None
            stats = protocol_logger.get_protocol_stats(db, since=since_ts)
            self.send_json(stats)

        elif path == '/api/v1/protocol/density':
            start = float(params.get('start', [str(time.time() - 3600)])[0])
            end = float(params.get('end', [str(time.time())])[0])
            buckets = int(params.get('buckets', ['100'])[0])
            density = protocol_logger.get_activity_density(
                db, start, end, buckets=min(buckets, 500))
            self.send_json({'density': density, 'start': start, 'end': end})

        elif path == '/api/v1/protocol/snapshot':
            at = float(params.get('at', [str(time.time())])[0])
            state = protocol_logger.get_state_at_time(db, at)
            self.send_json(state)

        # ── Provisioning ──
        elif path == '/api/v1/provision/bootstrap':
            from .provisioner import generate_bootstrap_script
            self_ip = os.environ.get('REPORT_IP', get_self_ip())
            cp_url = f'http://{self_ip}:{cfg.CONTROL_PLANE_PORT}'
            script = generate_bootstrap_script(cp_url)
            body = script.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_cors_headers()
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self._proto_resp_body = script[:512]
            self._proto_status = 200
            self._proto_content_length = len(body)
            return

        else:
            self.send_json({'error': 'Not found'}, 404)

    # ── POST Endpoints ────────────────────────────────────────────

    def do_POST(self):
        self._init_proto()
        try:
            self._handle_post()
        except Exception as e:
            log.error(f"POST {self.path} error: {e}", exc_info=True)
            try:
                self.send_json({'error': 'Internal server error'}, 500)
            except Exception:
                pass
        finally:
            self._log_protocol()

    def _handle_post(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        data = self.read_body()

        # ── Node Registration (was Gateway /register) ──
        if path == '/api/v1/register':
            node_id = data.get('id')
            if not node_id:
                self.send_json({'error': 'Missing node ID'}, 400)
                return

            node_type = data.get('type', 'drone')
            # v3 doesn't have separate orchestrators — skip orchestrator registration
            if node_type == 'orchestrator':
                self.send_json({'status': 'ignored', 'message': 'v3 does not track orchestrators'})
                return

            name = data.get('name', f'unknown-{node_id[:8]}')
            ip = data.get('ip') or self.client_address[0]
            caps = data.get('capabilities', {})
            metrics = data.get('metrics', {})

            db.upsert_node(
                node_id=node_id,
                name=name,
                ip=ip,
                node_type='sweeper' if name.lower().startswith(cfg.SWEEPER_PREFIX.lower()) else 'drone',
                cores=caps.get('cores'),
                ram_gb=caps.get('ram_gb'),
                capabilities=caps,
                metrics=metrics,
                current_task=data.get('current_task'),
                version=data.get('version'),
            )

            # Preserve paused state
            node = db.get_node(node_id)
            self_ip = os.environ.get('REPORT_IP', get_self_ip())

            # Emit registration event (only for first-time or returning drones)
            prev_status = node.get('status') if node else None
            if prev_status != 'online':
                cores_str = f" ({caps.get('cores', '?')} cores)" if caps.get('cores') else ""
                add_event('register', f"{name} came online{cores_str}",
                          {'drone_id': node_id, 'name': name, 'ip': ip, 'cores': caps.get('cores')})

            resp = {
                'status': 'registered',
                'orchestrator': self_ip,
                'orchestrator_port': cfg.CONTROL_PLANE_PORT,
                'orchestrator_name': 'build-swarm-v3',
            }
            if node and node.get('paused'):
                resp['paused'] = True

            self.send_json(resp)

        # ── Build Completion (was Orchestrator /complete) ──
        elif path == '/api/v1/complete':
            drone_id = data.get('id', 'unknown')
            package = data.get('package')
            status = data.get('status', 'unknown')
            duration = data.get('build_duration_s', 0)
            error_detail = data.get('error_detail', '')

            if not package:
                self.send_json({'error': 'Missing package'}, 400)
                return

            drone_name = db.get_drone_name(drone_id)

            # Handle success with validation
            if status == 'success':
                if not _is_virtual_package(package) and 'app-test/dummy-' not in package:
                    validated = _validate_binary(package)
                    if not validated:
                        status = 'missing_binary'

            # Record in DB
            db.complete_package(package, drone_id, status,
                               duration_seconds=duration,
                               error_message=error_detail)

            if status == 'success':
                health_monitor.record_success(drone_id)
                dur_str = f" in {duration:.1f}s" if duration > 0 else ""
                log.info(f"[RECV] {package} <- {drone_name}{dur_str}")
                add_event('complete', f"{package} completed on {drone_name}{dur_str}",
                          {'package': package, 'drone': drone_name, 'duration': duration})
            elif status == 'returned':
                log.info(f"[RETURNED] {package} by {drone_name} ({error_detail or 'unspecified'})")
                add_event('return', f"{package} returned by {drone_name}",
                          {'package': package, 'drone': drone_name, 'reason': error_detail})
            else:
                health_monitor.record_failure(drone_id)
                log.warning(f"[FAIL] {package} on {drone_name} ({status})")
                if error_detail:
                    log.warning(f"[FAIL] Detail: {error_detail[:200]}")
                add_event('fail', f"{package} failed on {drone_name}: {status}",
                          {'package': package, 'drone': drone_name, 'status': status,
                           'error': error_detail[:200] if error_detail else ''})

            self.send_json({'status': 'ok', 'accepted': status == 'success'})

        # ── Queue Packages (was Orchestrator /queue) ──
        elif path == '/api/v1/queue':
            packages = data.get('packages', [])
            if not packages:
                self.send_json({'error': 'No packages'}, 400)
                return

            session = db.get_active_session()
            session_id = session['id'] if session else None

            # Set portage timestamp if provided
            portage_ts = data.get('portage_timestamp')
            if portage_ts:
                db.set_config('expected_portage_timestamp', portage_ts)

            added = db.queue_packages(packages, session_id=session_id)
            log.info(f"Queued {added}/{len(packages)} packages")
            if added > 0:
                add_event('queue', f"{added} packages queued",
                          {'count': added, 'session_id': session_id})

            self.send_json({
                'status': 'ok',
                'queued': added,
                'session_id': session_id,
                'portage_timestamp': portage_ts,
            })

        # ── Control Actions (was Orchestrator /control) ──
        elif path == '/api/v1/control':
            action = data.get('action')
            self._handle_control(action, data)

        # ── Node Pause/Resume ──
        elif path.startswith('/api/v1/nodes/') and path.endswith('/pause'):
            target = path.split('/')[4]
            node = db.get_node(target) or db.get_node_by_name(target)
            if node:
                db.set_node_paused(node['id'], True)
                self.send_json({'status': 'paused', 'name': node['name']})
            else:
                self.send_json({'error': f'Node not found: {target}'}, 404)

        # ── Provisioning ──
        elif path == '/api/v1/provision/drone':
            from .provisioner import provision_drone_ssh
            ip = data.get('ip')
            name = data.get('name')
            if not ip:
                self.send_json({'error': 'Missing IP address'}, 400)
                return
            self_ip = os.environ.get('REPORT_IP', get_self_ip())
            cp_url = f'http://{self_ip}:{cfg.CONTROL_PLANE_PORT}'
            result = provision_drone_ssh(ip, cp_url, name)
            self.send_json(result)

        elif path.startswith('/api/v1/nodes/') and path.endswith('/resume'):
            target = path.split('/')[4]
            node = db.get_node(target) or db.get_node_by_name(target)
            if node:
                db.set_node_paused(node['id'], False)
                self.send_json({'status': 'resumed', 'name': node['name']})
            else:
                self.send_json({'error': f'Node not found: {target}'}, 404)

        else:
            self.send_json({'error': 'Not found'}, 404)

    def _handle_control(self, action: str, data: dict):
        """Handle control actions (pause/resume/unblock/reset/etc)."""
        if action == 'pause':
            db.set_config('paused', 'true')
            log.info("Control plane PAUSED")
            add_event('control', "Control plane paused")
            self.send_json({'status': 'paused'})

        elif action == 'resume':
            db.set_config('paused', 'false')
            log.info("Control plane RESUMED")
            add_event('control', "Control plane resumed")
            self.send_json({'status': 'active'})

        elif action == 'unblock':
            count = db.unblock_all()
            log.info(f"Unblocked {count} packages")
            add_event('unblock', f"{count} packages unblocked", {'count': count})
            self.send_json({'status': 'ok', 'unblocked': count})

        elif action == 'unground':
            drone_id = data.get('drone_id')
            if drone_id:
                health_monitor.unground_drone(drone_id)
            else:
                health_monitor.unground_all()
            self.send_json({'status': 'ok'})

        elif action == 'reset':
            session = db.get_active_session()
            if session:
                count = db.reset_queue(session['id'])
            else:
                count = db.reset_queue()
            db.reset_drone_health()
            log.info(f"Reset: {count} packages returned to needed")
            self.send_json({'status': 'reset', 'affected': count})

        elif action in ('rebalance', 'optimize'):
            # Reclaim all delegated back to needed
            delegated = db.get_delegated_packages()
            count = 0
            for pkg in delegated:
                db.reclaim_package(pkg['package'])
                count += 1
            log.info(f"Rebalanced: {count} packages reclaimed")
            self.send_json({'status': 'ok', 'reclaimed': count})

        elif action == 'clear_failures':
            db.execute(
                "UPDATE queue SET status = 'needed', failure_count = 0, "
                "error_message = NULL, assigned_to = NULL WHERE status IN ('blocked','failed')")
            log.info("Cleared all failures")
            self.send_json({'status': 'ok'})

        elif action == 'retry_failures':
            count = db.unblock_all()
            log.info(f"Retrying {count} failed packages")
            self.send_json({'status': 'ok', 'requeued': count})

        else:
            self.send_json({'error': f'Unknown action: {action}'}, 400)

    # ── DELETE Endpoints ──────────────────────────────────────────

    def do_DELETE(self):
        self._init_proto()
        try:
            self._handle_delete()
        except Exception as e:
            log.error(f"DELETE {self.path} error: {e}", exc_info=True)
            try:
                self.send_json({'error': 'Internal server error'}, 500)
            except Exception:
                pass
        finally:
            self._log_protocol()

    def _handle_delete(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        if path.startswith('/api/v1/nodes/'):
            node_id = path.split('/')[-1]
            if db.remove_node(node_id):
                self.send_json({'status': 'deleted', 'id': node_id})
            else:
                self.send_json({'error': 'Node not found'}, 404)
        else:
            self.send_json({'error': 'Not found'}, 404)


# ── Helper Functions ──────────────────────────────────────────────

def _is_virtual_package(package: str) -> bool:
    """Check if package is virtual (no binary produced)."""
    atom = package.lstrip('=')
    category = atom.split('/')[0] if '/' in atom else ''
    if category == 'virtual':
        return True
    virtual_patterns = ['clang-rtlib-config', 'eselect-ruby',
                        'openpgp-keys-', '-meta-']
    return any(p in atom for p in virtual_patterns)


def _validate_binary(package: str) -> bool:
    """Validate that a binary package exists in staging."""
    import glob as globmod

    atom = package.lstrip('=')
    cat = atom.split('/')[0]
    pv = atom.split('/')[-1]
    parts = pv.rsplit('-', 1)
    pkg_name = parts[0] if len(parts) == 2 else pv

    # Check v3 paths + v2 staging path for backwards compatibility
    search_bases = [cfg.STAGING_PATH, cfg.BINHOST_PATH,
                    '/var/cache/binpkgs-staging', '/var/cache/binpkgs']
    # Deduplicate while preserving order
    seen = set()
    bases = []
    for b in search_bases:
        if b not in seen:
            seen.add(b)
            bases.append(b)

    for base in bases:
        # Nested layout: cat/pkg_name/pv*.gpkg.tar (modern portage)
        # Flat layout: cat/pv*.gpkg.tar (older portage / v2 drones)
        patterns = [
            os.path.join(base, cat, pkg_name, f"{pv}*.gpkg.tar"),
            os.path.join(base, cat, f"{pv}*.gpkg.tar"),
        ]
        for pattern in patterns:
            matches = globmod.glob(pattern)
            for fpath in matches:
                size = os.path.getsize(fpath)
                if size >= 1024:
                    return True
                else:
                    log.error(f"Junk binary for {package}: {size}B at {fpath}")
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass

    return False


# ── Background Threads ────────────────────────────────────────────

_start_time = time.time()


def _metrics_recorder():
    """Record metrics snapshots every 15 seconds for charting."""
    while True:
        time.sleep(15)
        try:
            drones = db.get_online_drones()
            for d in drones:
                m = d.get('metrics', {})
                if m:
                    db.log_metrics(
                        node_id=d['id'],
                        cpu_percent=m.get('cpu_percent'),
                        ram_percent=m.get('ram_percent'),
                        load_1m=m.get('load_1m'),
                    )

            # Also log a system-wide entry (no node_id)
            db.log_metrics()

            # Prune old metrics every 100 cycles (~25 min)
            if int(time.time()) % 1500 < 15:
                db.prune_old_metrics(max_age_hours=24)

        except Exception as e:
            log.error(f"Metrics recorder error: {e}")


def _maintenance_loop():
    """Background maintenance: reclaim work, update status, auto-age blocks."""
    while True:
        time.sleep(15)
        try:
            db.update_node_status(cfg.NODE_TIMEOUT, cfg.STALE_TIMEOUT)
            scheduler.reclaim_offline_work()
            scheduler.auto_age_blocked()
        except Exception as e:
            log.error(f"Maintenance loop error: {e}")


def _protocol_prune_loop():
    """Prune protocol log entries older than 24h every 5 minutes."""
    while True:
        time.sleep(300)
        try:
            protocol_logger.prune_old_entries(db, max_age_hours=24)
        except Exception as e:
            log.error(f"Protocol prune error: {e}")


def _session_monitor():
    """Check if active session is complete."""
    while True:
        time.sleep(30)
        try:
            session = db.get_active_session()
            if not session:
                continue
            counts = db.get_queue_counts(session['id'])
            if counts['needed'] == 0 and counts['delegated'] == 0:
                if counts['received'] > 0 or counts['blocked'] > 0:
                    db.complete_session(session['id'])
                    log.info(f"Session {session['id']} completed: "
                             f"{counts['received']} received, {counts['blocked']} blocked")
        except Exception as e:
            log.error(f"Session monitor error: {e}")


# ── Server Startup ────────────────────────────────────────────────

def start(db_path: str = None, port: int = None):
    """Start the v3 control plane."""
    global db, health_monitor, scheduler, _start_time

    _start_time = time.time()
    port = port or cfg.CONTROL_PLANE_PORT
    db_path = db_path or cfg.DB_PATH

    # Setup logging
    cfg.setup_logging()

    # Initialize database
    db = SwarmDB(db_path)
    health_monitor = DroneHealthMonitor(db)
    scheduler = Scheduler(db, health_monitor)

    log.info(f"=== Build Swarm v3 Control Plane v{__version__} ===")
    log.info(f"Database: {db_path}")
    log.info(f"Port: {port}")
    log.info(f"Staging: {cfg.STAGING_PATH}")
    log.info(f"Binhost: {cfg.BINHOST_PATH}")

    # Ensure staging directories exist (skip if not writable, e.g. dev mode)
    for d in [cfg.STAGING_PATH, cfg.BINHOST_PATH]:
        try:
            os.makedirs(d, exist_ok=True)
        except PermissionError:
            log.warning(f"Cannot create {d} (permission denied) — binary validation will be skipped")

    # Initialize protocol logger
    protocol_logger.init(db)

    # Start background threads
    threading.Thread(target=_metrics_recorder, daemon=True).start()
    threading.Thread(target=_maintenance_loop, daemon=True).start()
    threading.Thread(target=_session_monitor, daemon=True).start()
    threading.Thread(target=_protocol_prune_loop, daemon=True).start()
    log.info("Background threads started (metrics, maintenance, session monitor, protocol prune)")

    # Start HTTP server
    server = ThreadingHTTPServer(('0.0.0.0', port), V3Handler)
    log.info(f"Listening on 0.0.0.0:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down control plane")
        server.shutdown()
