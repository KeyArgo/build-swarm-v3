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

        # Binhost stubs (Phase 5)
        if path == '/admin/api/binhost/flip':
            self.send_json({'status': 'not_implemented', 'message': 'Binhost flip — Phase 5'})
            return

        if path == '/admin/api/binhost/rsync':
            self.send_json({'status': 'not_implemented', 'message': 'Binhost rsync — Phase 5'})
            return

        self.send_error_json(404, f'Unknown admin endpoint: {path}')


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
