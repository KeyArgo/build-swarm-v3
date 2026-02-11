"""
Build Swarm v3 — Admin Dashboard Server

Serves the admin SPA and admin-only API endpoints on a secondary port (default 8093).
Shares the same process and database as the v3 control plane.
"""

import json
import logging
import os
import secrets
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from . import __version__
from . import config as cfg

log = logging.getLogger('swarm-v3')

# ── Admin secret management ──────────────────────────────────────

_admin_secret: str = ''

def _load_or_generate_secret() -> str:
    """Load admin secret from env, file, or auto-generate.

    Search order:
      1. ADMIN_SECRET environment variable
      2. /etc/build-swarm/admin.key
      3. ~/.local/share/build-swarm-v3/admin.key (non-root fallback)
    """
    # 1. Environment variable
    if cfg.ADMIN_SECRET:
        log.info("Admin key: loaded from ADMIN_SECRET env var")
        return cfg.ADMIN_SECRET

    # 2. Key file (production)
    key_file = Path('/etc/build-swarm/admin.key')
    if key_file.exists():
        try:
            secret = key_file.read_text().strip()
            log.info(f"Admin key: loaded from {key_file}")
            return secret
        except Exception:
            pass

    # 3. Key file (user dir fallback)
    alt_file = Path(cfg._DATA_DIR) / 'admin.key'
    if alt_file.exists():
        try:
            secret = alt_file.read_text().strip()
            log.info(f"Admin key: loaded from {alt_file}")
            return secret
        except Exception:
            pass

    # 4. Auto-generate and write
    secret = secrets.token_hex(16)
    for path in [key_file, alt_file]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secret + '\n')
            path.chmod(0o600)
            log.info(f"Admin key: generated and saved to {path}")
            return secret
        except (PermissionError, OSError):
            continue

    log.warning(f"Admin key: could not persist! Ephemeral key in use (lost on restart)")
    return secret


# ── MIME types ────────────────────────────────────────────────────

MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.json': 'application/json',
    '.svg':  'image/svg+xml',
    '.png':  'image/png',
    '.ico':  'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
}


# ── Admin HTTP Handler ────────────────────────────────────────────

class AdminHandler(BaseHTTPRequestHandler):
    """HTTP handler for admin dashboard: static files + admin API."""

    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        log.debug(f"[admin] {self.address_string()} - {format % args}")

    # ── Response helpers ──

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Admin-Key')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str):
        self.send_json({'error': message}, status)

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            return {}
        if length > 1_048_576:
            return {}
        if length == 0:
            return {}
        try:
            body = self.rfile.read(length).decode()
            return json.loads(body) if body else {}
        except Exception:
            return {}

    # ── Auth check ──

    def _check_auth(self) -> bool:
        """Check admin key for API routes. Static files are public."""
        key = self.headers.get('X-Admin-Key')
        if not key:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            key = params.get('key', [None])[0]
        return key == _admin_secret

    # ── Static file serving ──

    def _serve_static(self, path: str):
        """Serve a file from the admin/ directory."""
        if path in ('', '/'):
            path = '/index.html'

        static_dir = Path(cfg.ADMIN_STATIC_DIR)
        file_path = (static_dir / path.lstrip('/')).resolve()

        # Prevent path traversal
        if not str(file_path).startswith(str(static_dir.resolve())):
            self.send_error(403)
            return

        if not file_path.is_file():
            # SPA fallback: serve index.html for unknown paths
            file_path = static_dir / 'index.html'
            if not file_path.is_file():
                self.send_error(404)
                return

        ext = file_path.suffix
        content_type = MIME_TYPES.get(ext, 'application/octet-stream')

        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    # ── V2 proxy ──

    def _proxy_v2(self, v2_path: str):
        """Proxy a GET request to the v2 gateway."""
        url = f"{cfg.V2_GATEWAY_URL}{v2_path}"
        try:
            req = urllib.request.Request(url, method='GET')
            req.add_header('Accept', 'application/json')
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                self.send_json(data)
        except Exception as e:
            self.send_json({'error': f'v2 proxy failed: {e}', 'url': url}, 502)

    # ── GET handler ──

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Admin-Key')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = parse_qs(parsed.query)

        # Admin API routes (require auth)
        if path.startswith('/admin/api/'):
            if not self._check_auth():
                self.send_error_json(401, 'Unauthorized — provide X-Admin-Key header')
                return
            self._handle_admin_get(path, params)
            return

        # V2 proxy routes (require auth)
        if path.startswith('/v2/api/'):
            if not self._check_auth():
                self.send_error_json(401, 'Unauthorized')
                return
            v2_path = path.replace('/v2/api/', '/api/v1/', 1)
            self._proxy_v2(v2_path)
            return

        # Static files (no auth)
        self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        if not path.startswith('/admin/api/'):
            self.send_error(404)
            return

        if not self._check_auth():
            self.send_error_json(401, 'Unauthorized — provide X-Admin-Key header')
            return

        body = self.read_body()
        self._handle_admin_post(path, body)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        if not path.startswith('/admin/api/'):
            self.send_error(404)
            return

        if not self._check_auth():
            self.send_error_json(401, 'Unauthorized — provide X-Admin-Key header')
            return

        from . import control_plane as cp

        if path.startswith('/admin/api/drone-config/'):
            drone_name = path.split('/')[-1]
            if not drone_name:
                self.send_error_json(400, 'Missing drone name')
                return
            if cp.db:
                cp.db.delete_drone_config(drone_name)
                self.send_json({'status': 'ok', 'deleted': drone_name})
            else:
                self.send_error_json(500, 'Database not available')
            return

        # Allowlist deletion
        if path.startswith('/admin/api/drones/allowlist/'):
            entry_id = path.split('/')[-1]
            try:
                entry_id = int(entry_id)
            except (ValueError, TypeError):
                self.send_error_json(400, 'Invalid allowlist entry ID')
                return
            if cp.db:
                ok = cp.db.remove_allowlist(entry_id)
                if ok:
                    self.send_json({'status': 'ok', 'deleted': entry_id})
                else:
                    self.send_error_json(404, f'Allowlist entry {entry_id} not found')
            else:
                self.send_error_json(500, 'Database not available')
            return

        # Release deletion
        if path.startswith('/admin/api/releases/'):
            version = path.split('/')[-1]
            if cp.release_mgr:
                result = cp.release_mgr.delete_release(version)
                if result.get('status') == 'ok':
                    self.send_json(result)
                else:
                    self.send_error_json(400, result.get('error', 'Delete failed'))
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        self.send_error_json(404, f'Unknown admin endpoint: {path}')

    # ── Admin GET endpoints ──

    def _handle_admin_get(self, path: str, params: dict):
        # Import control plane globals (shared process)
        from . import control_plane as cp

        if path == '/admin/api/system/info':
            uptime = time.time() - cp._start_time
            db_path = Path(cp.db.db_path) if cp.db else Path('unknown')
            db_size = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0
            self.send_json({
                'version': __version__,
                'uptime_s': round(uptime),
                'uptime_human': f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m",
                'db_path': str(db_path),
                'db_size_mb': round(db_size, 2),
                'control_plane_port': cfg.CONTROL_PLANE_PORT,
                'admin_port': cfg.ADMIN_PORT,
                'v2_gateway_url': cfg.V2_GATEWAY_URL,
                'binhost_primary_ip': cfg.BINHOST_PRIMARY_IP,
                'binhost_secondary_ip': cfg.BINHOST_SECONDARY_IP,
            })
            return

        if path == '/admin/api/config':
            if cp.db:
                rows = cp.db.execute("SELECT key, value, updated_at FROM config ORDER BY key").fetchall()
                config = {r['key']: {'value': r['value'], 'updated_at': r['updated_at']} for r in rows}
            else:
                config = {}
            self.send_json(config)
            return

        if path == '/admin/api/auth/check':
            self.send_json({'authenticated': True, 'version': __version__})
            return

        # ── Drone config ──

        if path == '/admin/api/drone-configs':
            # List all drone configs
            if cp.db:
                configs = cp.db.get_all_drone_configs()
            else:
                configs = []
            self.send_json(configs)
            return

        if path.startswith('/admin/api/drone-config/'):
            # Get config for a specific drone
            drone_name = path.split('/')[-1]
            if cp.db:
                config = cp.db.get_drone_config(drone_name)
                if config:
                    self.send_json(config)
                else:
                    # Return defaults for unconfigured drone
                    self.send_json({
                        'node_name': drone_name,
                        'ssh_user': 'root',
                        'ssh_port': 22,
                        'ssh_key_path': None,
                        'ssh_password': None,
                        'cores_limit': None,
                        'emerge_jobs': 2,
                        'ram_limit_gb': None,
                        'auto_reboot': 1,
                        'protected': 0,
                        'max_failures': None,
                        'binhost_upload_url': None,
                        'display_name': None,
                        'v2_name': None,
                        'control_plane': 'v3',
                        'locked': 1,
                        'notes': None,
                        '_unconfigured': True,
                    })
            else:
                self.send_error_json(500, 'Database not available')
            return

        # ── Releases ──

        if path == '/admin/api/releases':
            if cp.release_mgr:
                self.send_json({'releases': cp.release_mgr.list_releases()})
            else:
                self.send_json({'releases': []})
            return

        if path == '/admin/api/releases/diff':
            from_v = params.get('from', [None])[0]
            to_v = params.get('to', [None])[0]
            if not from_v or not to_v:
                self.send_error_json(400, 'Both "from" and "to" parameters required')
                return
            if cp.release_mgr:
                self.send_json(cp.release_mgr.diff_releases(from_v, to_v))
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        if path.startswith('/admin/api/releases/') and path.endswith('/packages'):
            version = path.split('/')[-2]
            if cp.release_mgr:
                pkgs = cp.release_mgr.get_release_packages(version)
                self.send_json({'version': version, 'packages': pkgs})
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        if path.startswith('/admin/api/releases/'):
            version = path.split('/')[-1]
            if cp.release_mgr:
                release = cp.release_mgr.get_release(version)
                if release:
                    self.send_json(release)
                else:
                    self.send_error_json(404, f'Release not found: {version}')
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        # ── Drone Allowlist ──

        if path == '/admin/api/drones/allowlist':
            if cp.db:
                drone = params.get('drone', [None])[0]
                entries = cp.db.get_allowlist(drone)
                self.send_json({'allowlist': entries})
            else:
                self.send_error_json(500, 'Database not available')
            return

        if path.startswith('/admin/api/drones/') and path.endswith('/packages'):
            drone_name = path.split('/')[4]
            self._handle_drone_packages(cp, drone_name)
            return

        if path.startswith('/admin/api/drones/') and path.endswith('/audit'):
            drone_name = path.split('/')[4]
            self._handle_drone_audit(cp, drone_name)
            return

        # Per-drone log viewer
        if path.startswith('/admin/api/drones/') and path.endswith('/log'):
            drone_name = path.split('/')[4]
            self._handle_drone_log(cp, drone_name, params)
            return

        # Drone version management
        if path == '/admin/api/drones/versions':
            self._handle_drone_versions(cp)
            return

        if path == '/admin/api/drones/payload':
            self._handle_drone_payload()
            return

        # V2 proxy (GET endpoints)
        if path == '/admin/api/v2/nodes':
            self._proxy_v2('/api/v1/nodes?all=true')
            return

        if path == '/admin/api/v2/status':
            self._proxy_v2('/api/v1/status')
            return

        self.send_error_json(404, f'Unknown admin endpoint: {path}')

    # ── Admin POST endpoints ──

    def _handle_admin_post(self, path: str, body: dict):
        from . import control_plane as cp

        if path == '/admin/api/config':
            key = body.get('key')
            value = body.get('value')
            if not key:
                self.send_error_json(400, 'Missing "key"')
                return
            if cp.db:
                cp.db.execute(
                    "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                    (key, str(value))
                )
                cp.db.conn.commit()
            self.send_json({'status': 'ok', 'key': key, 'value': value})
            return

        # ── Drone config CRUD ──

        if path.startswith('/admin/api/drone-config/'):
            drone_name = path.split('/')[-1]
            if not drone_name:
                self.send_error_json(400, 'Missing drone name')
                return
            if not cp.db:
                self.send_error_json(500, 'Database not available')
                return

            # Whitelist of allowed fields
            allowed = {
                'ssh_user', 'ssh_port', 'ssh_key_path', 'ssh_password',
                'cores_limit', 'emerge_jobs', 'ram_limit_gb',
                'auto_reboot', 'protected', 'max_failures', 'binhost_upload_url',
                'display_name', 'v2_name', 'control_plane', 'locked', 'notes',
            }
            fields = {k: v for k, v in body.items() if k in allowed}
            if not fields:
                self.send_error_json(400, 'No valid fields provided')
                return

            result = cp.db.upsert_drone_config(drone_name, **fields)
            self.send_json(result)
            return

        # Reset upload failures for a drone
        if path.startswith('/admin/api/drone/') and path.endswith('/reset-upload'):
            drone_name = path.split('/')[4]
            if cp.db:
                # Find the drone's node ID
                node = cp.db.fetchone("SELECT id FROM nodes WHERE name = ?", (drone_name,))
                if node:
                    cp.db.reset_upload_failures(node['id'])
                    self.send_json({'status': 'ok', 'drone': drone_name, 'message': 'Upload failures reset'})
                else:
                    self.send_error_json(404, f'Drone not found: {drone_name}')
            else:
                self.send_error_json(500, 'Database not available')
            return

        # Drone lock/unlock/audit stubs (will be implemented in Phase 4)
        if path.startswith('/admin/api/drone/') and path.endswith('/lock'):
            drone_name = path.split('/')[4]
            self.send_json({'status': 'not_implemented', 'drone': drone_name, 'message': 'Drone lock endpoint — Phase 4'})
            return

        if path.startswith('/admin/api/drone/') and path.endswith('/unlock'):
            drone_name = path.split('/')[4]
            timer = body.get('timer_minutes', 0)
            self.send_json({'status': 'not_implemented', 'drone': drone_name, 'timer': timer, 'message': 'Drone unlock endpoint — Phase 4'})
            return

        # ── Releases ──

        if path == '/admin/api/releases':
            if not cp.release_mgr:
                self.send_error_json(500, 'Release manager not available')
                return
            result = cp.release_mgr.create_release(
                version=body.get('version'),
                name=body.get('name'),
                notes=body.get('notes'),
                created_by=body.get('created_by', 'admin'),
            )
            status_code = 201 if result.get('status') == 'ok' else 400
            self.send_json(result, status_code)
            return

        if path == '/admin/api/releases/rollback':
            if cp.release_mgr:
                self.send_json(cp.release_mgr.rollback())
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        if path == '/admin/api/releases/migrate':
            if cp.release_mgr:
                self.send_json(cp.release_mgr.migrate_to_release_system())
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        if path.startswith('/admin/api/releases/') and path.endswith('/promote'):
            version = path.split('/')[-2]
            if cp.release_mgr:
                self.send_json(cp.release_mgr.promote_release(version))
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        if path.startswith('/admin/api/releases/') and path.endswith('/archive'):
            version = path.split('/')[-2]
            if cp.release_mgr:
                self.send_json(cp.release_mgr.archive_release(version))
            else:
                self.send_error_json(500, 'Release manager not available')
            return

        # ── Drone Allowlist + Clean ──

        if path == '/admin/api/drones/allowlist':
            package = body.get('package')
            if not package:
                self.send_error_json(400, 'Missing "package"')
                return
            if cp.db:
                entry_id = cp.db.add_allowlist(
                    package=package,
                    drone_name=body.get('drone'),
                    reason=body.get('reason'),
                    added_by=body.get('added_by', 'admin'),
                )
                self.send_json({'status': 'ok', 'id': entry_id, 'package': package})
            else:
                self.send_error_json(500, 'Database not available')
            return

        if path.startswith('/admin/api/drones/') and path.endswith('/clean'):
            drone_name = path.split('/')[4]
            self._handle_drone_clean(cp, drone_name, body)
            return

        # Binhost stubs (Phase 5)
        if path == '/admin/api/binhost/flip':
            self.send_json({'status': 'not_implemented', 'message': 'Binhost flip — Phase 5'})
            return

        if path == '/admin/api/binhost/rsync':
            self.send_json({'status': 'not_implemented', 'message': 'Binhost rsync — Phase 5'})
            return

        self.send_error_json(404, f'Unknown admin endpoint: {path}')


    # ── Drone management helpers ────────────────────────────────────

    def _handle_drone_packages(self, cp, drone_name: str):
        """List installed packages on a drone via SSH."""
        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        ip = node.get('ip')
        if not ip:
            self.send_error_json(400, f'No IP for drone {drone_name}')
            return

        try:
            import subprocess
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'ls -d /var/db/pkg/*/* 2>/dev/null | '
                 'sed "s|/var/db/pkg/||" | sort'],
                capture_output=True, text=True, timeout=15)
            packages = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]

            # Also get @world and profile
            result2 = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'cat /var/lib/portage/world 2>/dev/null; '
                 'echo "---PROFILE---"; '
                 'eselect profile show 2>/dev/null | tail -1'],
                capture_output=True, text=True, timeout=15)

            parts = result2.stdout.split('---PROFILE---')
            world = [w.strip() for w in parts[0].strip().split('\n') if w.strip()] if parts else []
            profile = parts[1].strip() if len(parts) > 1 else 'unknown'

            self.send_json({
                'drone': drone_name,
                'ip': ip,
                'installed_count': len(packages),
                'installed': packages,
                'world_count': len(world),
                'world': world,
                'profile': profile,
            })
        except Exception as e:
            self.send_error_json(500, f'SSH failed: {e}')

    def _handle_drone_versions(self, cp):
        """Show version info for all drones + payload status."""
        import time
        nodes = cp.db.get_all_nodes(include_offline=True) if cp.db else []
        drones = []
        for n in nodes:
            if n['type'] != 'drone':
                continue
            last_seen = n.get('last_seen')
            ago = round(time.time() - last_seen) if last_seen else None
            drones.append({
                'name': n['name'],
                'id': n['id'],
                'ip': n.get('ip'),
                'version': n.get('version'),
                'status': n.get('status'),
                'last_seen_ago_s': ago,
            })

        # Check payload manifest from v2 gateway
        payload_info = None
        try:
            import urllib.request
            url = f"{cfg.V2_GATEWAY_URL}/api/v1/payload/manifest"
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload_info = json.loads(resp.read().decode())
        except Exception:
            pass

        self.send_json({
            'drones': drones,
            'payload': {
                'available': payload_info is not None,
                'version': payload_info.get('version') if payload_info else None,
                'component_count': len(payload_info.get('components', {})) if payload_info else 0,
            } if payload_info else {'available': False},
        })

    def _handle_drone_payload(self):
        """Get the full payload manifest from v2 gateway."""
        try:
            import urllib.request
            url = f"{cfg.V2_GATEWAY_URL}/api/v1/payload/manifest"
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                manifest = json.loads(resp.read().decode())
            self.send_json(manifest)
        except Exception as e:
            self.send_json({'error': f'Failed to fetch payload manifest: {e}',
                            'hint': 'The v2 gateway must be running to serve payloads'})

    def _handle_drone_log(self, cp, drone_name: str, params: dict):
        """Combined event + build history log for a specific drone."""
        from .events import get_events_db
        import time

        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        drone_id = node['id']
        limit = int(params.get('limit', ['200'])[0])
        since_h = float(params.get('hours', ['24'])[0])
        since_ts = time.time() - since_h * 3600

        # Events for this drone (search by name since events store drone name)
        events = get_events_db(since_ts=since_ts, drone_id=drone_name, limit=limit)

        # Build history for this drone
        builds = cp.db.fetchall("""
            SELECT package, status, drone_name, duration_seconds,
                   built_at, error_message, session_id
            FROM build_history
            WHERE drone_id = ? AND built_at > ?
            ORDER BY built_at DESC LIMIT ?
        """, (drone_id, since_ts, limit))

        # Connection timeline: find register events to track online/offline
        connection_events = [e for e in events if e['type'] in ('register', 'stale', 'control')]

        self.send_json({
            'drone': drone_name,
            'drone_id': drone_id,
            'since': since_ts,
            'events': events,
            'builds': [dict(b) for b in builds],
            'connections': connection_events,
        })

    def _handle_drone_audit(self, cp, drone_name: str):
        """Audit a drone: compare installed packages against allowlist."""
        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        ip = node.get('ip')
        if not ip:
            self.send_error_json(400, f'No IP for drone {drone_name}')
            return

        try:
            import subprocess
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'cat /var/lib/portage/world 2>/dev/null; '
                 'echo "---PROFILE---"; '
                 'eselect profile show 2>/dev/null | tail -1; '
                 'echo "---COUNT---"; '
                 'ls -d /var/db/pkg/*/* 2>/dev/null | wc -l'],
                capture_output=True, text=True, timeout=15)

            parts = result.stdout.split('---PROFILE---')
            world = [w.strip() for w in parts[0].strip().split('\n') if w.strip()] if parts else []

            profile_and_count = parts[1].split('---COUNT---') if len(parts) > 1 else ['unknown', '0']
            profile = profile_and_count[0].strip()
            total_count = int(profile_and_count[1].strip()) if len(profile_and_count) > 1 else 0

            # Get allowlist
            allowed = cp.db.get_allowlist_packages(drone_name)

            # Compute excess (in @world but not in allowlist)
            excess = sorted(set(world) - allowed)
            # Missing (in allowlist but not in @world)
            missing = sorted(allowed - set(world))

            # Profile check
            is_base_profile = 'desktop' not in profile and 'gnome' not in profile

            self.send_json({
                'drone': drone_name,
                'ip': ip,
                'total_installed': total_count,
                'world_count': len(world),
                'world': world,
                'profile': profile,
                'is_base_profile': is_base_profile,
                'allowed_count': len(allowed),
                'allowed': sorted(allowed),
                'excess_count': len(excess),
                'excess': excess,
                'missing_count': len(missing),
                'missing': missing,
                'clean': len(excess) == 0 and is_base_profile,
            })
        except Exception as e:
            self.send_error_json(500, f'SSH failed: {e}')

    def _handle_drone_clean(self, cp, drone_name: str, body: dict):
        """Clean a drone: switch to base profile, write minimal @world, depclean."""
        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        ip = node.get('ip')
        if not ip:
            self.send_error_json(400, f'No IP for drone {drone_name}')
            return

        dry_run = body.get('dry_run', False)

        # Get allowlist for this drone
        allowed = cp.db.get_allowlist_packages(drone_name)
        world_content = '\n'.join(sorted(allowed))

        try:
            import subprocess
            steps = []

            if dry_run:
                steps.append(f'Would write {len(allowed)} packages to @world')
                steps.append('Would switch to base profile')
                steps.append('Would run emerge --depclean')
                self.send_json({
                    'status': 'dry_run',
                    'drone': drone_name,
                    'steps': steps,
                    'world_packages': sorted(allowed),
                })
                return

            # Step 1: Write @world
            subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 f'echo "{world_content}" > /var/lib/portage/world'],
                capture_output=True, text=True, timeout=15)
            steps.append(f'Wrote {len(allowed)} packages to @world')

            # Step 2: Switch profile
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'eselect profile set default/linux/amd64/23.0 2>&1'],
                capture_output=True, text=True, timeout=15)
            steps.append(f'Profile switch: {result.stdout.strip() or "done"}')

            # Step 3: Depclean (background, can take a while)
            subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'nohup emerge --depclean --ask=n > /tmp/depclean.log 2>&1 &'],
                capture_output=True, text=True, timeout=15)
            steps.append('Started depclean in background (check /tmp/depclean.log on drone)')

            self.send_json({
                'status': 'ok',
                'drone': drone_name,
                'steps': steps,
                'world_packages': sorted(allowed),
            })
        except Exception as e:
            self.send_error_json(500, f'Clean failed: {e}')


# ── Server startup ────────────────────────────────────────────────

def start_admin_server(port: int = None):
    """Start the admin dashboard HTTP server (called from control_plane.start)."""
    global _admin_secret

    port = port or cfg.ADMIN_PORT
    _admin_secret = _load_or_generate_secret()

    static_dir = Path(cfg.ADMIN_STATIC_DIR)
    if not static_dir.exists():
        log.warning(f"Admin static dir not found: {static_dir}")
        log.warning("Admin dashboard will return 404 for static files")

    server = ThreadingHTTPServer(('0.0.0.0', port), AdminHandler)

    # Find where the key lives for the log message
    key_path = None
    for p in [Path('/etc/build-swarm/admin.key'), Path(cfg._DATA_DIR) / 'admin.key']:
        if p.exists():
            try:
                if p.read_text().strip() == _admin_secret:
                    key_path = p
                    break
            except Exception:
                pass

    log.info(f"Admin dashboard on http://0.0.0.0:{port}/")
    if key_path:
        log.info(f"Admin key: {_admin_secret[:4]}...{_admin_secret[-4:]}  (cat {key_path})")
    else:
        log.info(f"Admin key: {_admin_secret[:4]}...{_admin_secret[-4:]}  (from env var or ephemeral)")

    server.serve_forever()
