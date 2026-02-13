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
from .db import CRITICAL_PACKAGES

log = logging.getLogger('swarm-v3')

# ── Admin secret management ──────────────────────────────────────

_admin_secret: str = ''

# ── Clean preflight tokens (in-memory, keyed by token string) ────
# Each value: {'drone': name, 'expires': timestamp, 'diff': {...}}
_preflight_tokens: dict = {}

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
                try:
                    ok = cp.db.remove_allowlist(entry_id)
                except ValueError as e:
                    self.send_error_json(403, str(e))
                    return
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

        # Per-drone log viewer (events + build history)
        if path.startswith('/admin/api/drones/') and path.endswith('/log'):
            drone_name = path.split('/')[4]
            self._handle_drone_log(cp, drone_name, params)
            return

        # Per-drone system log viewer (via SSH)
        if path.startswith('/admin/api/drones/') and path.endswith('/syslog'):
            drone_name = path.split('/')[4]
            self._handle_drone_syslog(cp, drone_name, params)
            return

        # Control plane log viewer
        if path == '/admin/api/logs/control-plane':
            self._handle_control_plane_log(params)
            return

        # Per-drone escalation state (v4)
        if path.startswith('/admin/api/drones/') and path.endswith('/escalation'):
            drone_name = path.split('/')[4]
            self._handle_drone_escalation(cp, drone_name)
            return

        # Per-drone ping (v4)
        if path.startswith('/admin/api/drones/') and path.endswith('/ping'):
            drone_name = path.split('/')[4]
            self._handle_drone_ping(cp, drone_name)
            return

        # Self-healing status (v4)
        if path == '/admin/api/self-healing/status':
            self._handle_self_healing_status(cp)
            return

        # Drone version management
        if path == '/admin/api/drones/versions':
            self._handle_drone_versions(cp)
            return

        if path == '/admin/api/drones/payload':
            self._handle_drone_payload()
            return

        # Payload versioning (v4)
        if path == '/admin/api/payloads':
            self._handle_payloads_list(cp)
            return

        if path == '/admin/api/payloads/status':
            self._handle_payloads_status(cp)
            return

        if path.startswith('/admin/api/payloads/') and '/versions' in path:
            # GET /admin/api/payloads/<type>/versions
            payload_type = path.split('/')[4]
            self._handle_payload_versions(cp, payload_type)
            return

        if path.startswith('/admin/api/payloads/') and '/deploy-log' in path:
            # GET /admin/api/payloads/<type>/deploy-log
            payload_type = path.split('/')[4]
            self._handle_payload_deploy_log(cp, payload_type, params)
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

        # ── Payload Versioning (v4) ──

        if path == '/admin/api/payloads':
            self._handle_payload_register(cp, body)
            return

        if path.startswith('/admin/api/payloads/') and path.endswith('/deploy'):
            # POST /admin/api/payloads/<type>/<version>/deploy
            parts = path.split('/')
            payload_type = parts[4]
            version = parts[5]
            self._handle_payload_deploy(cp, payload_type, version, body)
            return

        if path.startswith('/admin/api/payloads/') and path.endswith('/rolling-deploy'):
            # POST /admin/api/payloads/<type>/<version>/rolling-deploy
            parts = path.split('/')
            payload_type = parts[4]
            version = parts[5]
            self._handle_payload_rolling_deploy(cp, payload_type, version, body)
            return

        if path.startswith('/admin/api/payloads/') and path.endswith('/verify'):
            # POST /admin/api/payloads/<type>/verify
            payload_type = path.split('/')[4]
            drone_name = body.get('drone')
            self._handle_payload_verify(cp, payload_type, drone_name)
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

        if path.startswith('/admin/api/drones/') and path.endswith('/clean/preflight'):
            drone_name = path.split('/')[4]
            self._handle_clean_preflight(cp, drone_name)
            return

        if path.startswith('/admin/api/drones/') and path.endswith('/clean/execute'):
            drone_name = path.split('/')[4]
            self._handle_clean_execute(cp, drone_name, body)
            return

        # Legacy /clean endpoint — redirect to new flow
        if path.startswith('/admin/api/drones/') and path.endswith('/clean'):
            self.send_error_json(410, 'The /clean endpoint has been replaced. Use /clean/preflight then /clean/execute.')
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

    # ── Payload Versioning Handlers (v4) ──

    def _handle_payloads_list(self, cp):
        """List all payload versions."""
        if not cp.db:
            self.send_error_json(500, 'Database not available')
            return
        versions = cp.db.get_payload_versions(limit=100)
        self.send_json({'versions': versions})

    def _handle_payloads_status(self, cp):
        """Get deployment status across all drones."""
        from .payloads import get_manager
        mgr = get_manager()
        if not mgr:
            self.send_error_json(500, 'Payload manager not initialized')
            return
        status = mgr.get_deployment_status()
        self.send_json(status)

    def _handle_payload_versions(self, cp, payload_type: str):
        """List versions for a specific payload type."""
        if not cp.db:
            self.send_error_json(500, 'Database not available')
            return
        versions = cp.db.get_payload_versions(payload_type=payload_type, limit=50)
        latest = cp.db.get_latest_payload_version(payload_type)
        self.send_json({
            'payload_type': payload_type,
            'versions': versions,
            'latest': latest,
        })

    def _handle_payload_deploy_log(self, cp, payload_type: str, params: dict):
        """Get deployment log for a payload type."""
        if not cp.db:
            self.send_error_json(500, 'Database not available')
            return
        limit = int(params.get('limit', ['100'])[0])
        # Filter by drone if specified
        drone = params.get('drone', [None])[0]
        if drone:
            node = cp.db.get_node_by_name(drone)
            if node:
                history = cp.db.get_payload_deploy_history(drone_id=node['id'], limit=limit)
            else:
                history = []
        else:
            history = cp.db.get_payload_deploy_history(limit=limit)
        # Filter by type
        history = [h for h in history if h.get('payload_type') == payload_type]
        self.send_json({'payload_type': payload_type, 'history': history})

    def _handle_payload_register(self, cp, body: dict):
        """Register a new payload version."""
        from .payloads import get_manager
        import base64

        mgr = get_manager()
        if not mgr:
            self.send_error_json(500, 'Payload manager not initialized')
            return

        payload_type = body.get('type')
        version = body.get('version')
        content_b64 = body.get('content')  # Base64 encoded

        if not all([payload_type, version, content_b64]):
            self.send_error_json(400, 'Missing required fields: type, version, content')
            return

        try:
            content = base64.b64decode(content_b64)
        except Exception:
            self.send_error_json(400, 'Invalid base64 content')
            return

        try:
            result = mgr.register_version(
                payload_type=payload_type,
                version=version,
                content=content,
                description=body.get('description'),
                notes=body.get('notes'),
                created_by=body.get('created_by', 'admin'),
            )
            self.send_json({'status': 'ok', 'version': result}, 201)
        except ValueError as e:
            self.send_error_json(409, str(e))
        except Exception as e:
            self.send_error_json(500, str(e))

    def _handle_payload_deploy(self, cp, payload_type: str, version: str, body: dict):
        """Deploy a payload to a single drone."""
        from .payloads import get_manager

        mgr = get_manager()
        if not mgr:
            self.send_error_json(500, 'Payload manager not initialized')
            return

        drone_name = body.get('drone')
        if not drone_name:
            self.send_error_json(400, 'Missing "drone" field')
            return

        success, message = mgr.deploy_to_drone(
            drone_name=drone_name,
            payload_type=payload_type,
            version=version,
            deployed_by=body.get('deployed_by', 'admin'),
            verify=body.get('verify', True),
        )

        self.send_json({
            'status': 'ok' if success else 'error',
            'drone': drone_name,
            'payload_type': payload_type,
            'version': version,
            'message': message,
        }, 200 if success else 500)

    def _handle_payload_rolling_deploy(self, cp, payload_type: str, version: str, body: dict):
        """Rolling deploy to multiple drones."""
        from .payloads import get_manager

        mgr = get_manager()
        if not mgr:
            self.send_error_json(500, 'Payload manager not initialized')
            return

        drone_names = body.get('drones')  # None = all outdated drones
        results = mgr.rolling_deploy(
            payload_type=payload_type,
            version=version,
            drone_names=drone_names,
            deployed_by=body.get('deployed_by', 'admin'),
            health_check=body.get('health_check', True),
            rollback_on_fail=body.get('rollback_on_fail', True),
        )

        success_count = sum(1 for s, _ in results.values() if s)
        fail_count = len(results) - success_count

        self.send_json({
            'status': 'ok' if fail_count == 0 else 'partial',
            'payload_type': payload_type,
            'version': version,
            'success_count': success_count,
            'fail_count': fail_count,
            'results': {name: {'success': s, 'message': m} for name, (s, m) in results.items()},
        })

    def _handle_payload_verify(self, cp, payload_type: str, drone_name: str):
        """Verify payload on a drone matches expected hash."""
        from .payloads import get_manager

        mgr = get_manager()
        if not mgr:
            self.send_error_json(500, 'Payload manager not initialized')
            return

        if not drone_name:
            self.send_error_json(400, 'Missing "drone" field')
            return

        matches, remote_hash = mgr.verify_drone_payload(drone_name, payload_type)
        self.send_json({
            'drone': drone_name,
            'payload_type': payload_type,
            'matches': matches,
            'remote_hash': remote_hash if isinstance(remote_hash, str) and len(remote_hash) == 64 else None,
            'message': remote_hash if not matches else 'Hash matches',
        })

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

    def _handle_drone_syslog(self, cp, drone_name: str, params: dict):
        """Fetch system logs from a drone via SSH."""
        import subprocess

        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            self.send_error_json(400, f'No IP for drone {drone_name}')
            return

        lines = int(params.get('lines', ['100'])[0])
        log_type = params.get('type', ['swarm-drone'])[0]

        # Map log type to file/command
        log_sources = {
            'swarm-drone': '/var/log/swarm-drone.log',
            'emerge': '/var/log/emerge.log',
            'portage': '/var/log/portage/elog/summary.log',
            'messages': '/var/log/messages',
            'dmesg': 'dmesg | tail -n',
        }

        log_path = log_sources.get(log_type, log_sources['swarm-drone'])

        # Build SSH command
        ssh_cfg = cp.db.fetchone(
            "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
            (drone_name,)) if cp.db else None

        user = 'root'
        port = 22
        key_path = None

        if ssh_cfg:
            user = ssh_cfg['ssh_user'] or 'root'
            port = ssh_cfg['ssh_port'] or 22
            key_path = ssh_cfg['ssh_key_path']

        cmd = [
            'ssh', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=no',
            '-o', 'BatchMode=yes',
        ]
        if port != 22:
            cmd.extend(['-p', str(port)])
        if key_path:
            cmd.extend(['-i', key_path])
        cmd.append(f'{user}@{ip}')

        if log_type == 'dmesg':
            cmd.append(f'dmesg | tail -n {lines}')
        else:
            cmd.append(f'tail -n {lines} {log_path} 2>/dev/null || echo "Log file not found"')

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            self.send_json({
                'drone': drone_name,
                'log_type': log_type,
                'lines': lines,
                'content': result.stdout,
                'error': result.stderr if result.returncode != 0 else None,
            })
        except subprocess.TimeoutExpired:
            self.send_error_json(504, 'SSH timeout')
        except Exception as e:
            self.send_error_json(500, str(e))

    def _handle_control_plane_log(self, params: dict):
        """Read control plane log file."""
        lines = int(params.get('lines', ['200'])[0])
        lines = min(lines, 5000)  # Cap at 5000 lines

        log_file = cfg.LOG_FILE
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    all_lines = f.readlines()
                    content = ''.join(all_lines[-lines:])
            else:
                content = f"Log file not found: {log_file}"

            self.send_json({
                'log_file': log_file,
                'lines': lines,
                'content': content,
            })
        except Exception as e:
            self.send_error_json(500, str(e))

    def _handle_drone_escalation(self, cp, drone_name: str):
        """Get escalation state for a drone."""
        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        # Get escalation state from database
        state = cp.db.get_escalation_state(node['id']) if cp.db else {}

        # Get last probe result
        health = cp.db.get_drone_health(node['id']) if cp.db else {}
        probe = None
        try:
            probe_json = health.get('last_probe_result')
            if probe_json:
                probe = json.loads(probe_json) if isinstance(probe_json, str) else probe_json
        except Exception:
            pass

        self.send_json({
            'drone': drone_name,
            'escalation': state,
            'health': {
                'failures': health.get('failures', 0),
                'grounded_until': health.get('grounded_until'),
                'last_failure': health.get('last_failure'),
            },
            'last_probe': probe,
            'last_probe_at': health.get('last_probe_at'),
        })

    def _handle_drone_ping(self, cp, drone_name: str):
        """Send proof-of-life ping to a drone."""
        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        if cp.proof_of_life:
            result = cp.proof_of_life.ping(node['id'])
            self.send_json(result)
        else:
            self.send_error_json(500, 'Proof of life prober not available')

    def _handle_self_healing_status(self, cp):
        """Get overall self-healing status."""
        result = {
            'enabled': cp.self_healing is not None,
            'drones': {},
        }

        if cp.self_healing:
            # Get escalation states
            escalation_state = cp.self_healing.get_escalation_state()
            for drone_id, state in escalation_state.items():
                name = cp.db.get_drone_name(drone_id) if cp.db else drone_id
                result['drones'][name] = {
                    'drone_id': drone_id,
                    **state,
                }

        # Also get health status for all drones
        if cp.db:
            nodes = cp.db.get_all_nodes(include_offline=True)
            for node in nodes:
                if node['type'] not in ('drone', 'sweeper'):
                    continue
                name = node['name']
                if name not in result['drones']:
                    result['drones'][name] = {
                        'drone_id': node['id'],
                        'level': 0,
                    }
                # Add health info
                health = cp.db.get_drone_health(node['id'])
                result['drones'][name]['failures'] = health.get('failures', 0)
                result['drones'][name]['grounded_until'] = health.get('grounded_until')
                result['drones'][name]['status'] = node['status']

        self.send_json(result)

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

    def _handle_clean_preflight(self, cp, drone_name: str):
        """Phase 1 of clean: run all safety checks, compute diff, issue token.

        Returns a one-time preflight_token (valid 5 minutes) that must be
        passed to /clean/execute along with the typed drone name.
        """
        import subprocess

        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if not node:
            self.send_error_json(404, f'Drone not found: {drone_name}')
            return

        ip = node.get('ip')
        if not ip:
            self.send_error_json(400, f'No IP for drone {drone_name}')
            return

        checks = []

        # Check 1: Drone not actively building
        current_task = node.get('current_task')
        delegated = cp.db.get_delegated_packages(node['id']) if cp.db else []
        is_building = bool(current_task) or any(p.get('building_since') for p in delegated)
        checks.append({
            'name': 'not_building',
            'passed': not is_building,
            'detail': f'Currently building: {current_task}' if is_building else 'Idle',
        })

        # Check 2: SSH reachable
        ssh_ok = False
        try:
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 '-o', 'BatchMode=yes', f'root@{ip}', 'echo ok'],
                capture_output=True, text=True, timeout=10)
            ssh_ok = result.returncode == 0 and 'ok' in result.stdout
        except Exception:
            pass
        checks.append({
            'name': 'ssh_reachable',
            'passed': ssh_ok,
            'detail': f'SSH to root@{ip}' + (' OK' if ssh_ok else ' FAILED'),
        })

        if not ssh_ok:
            self.send_json({
                'status': 'preflight_failed',
                'drone': drone_name,
                'checks': checks,
                'error': 'Cannot reach drone via SSH — aborting preflight',
            })
            return

        # Check 3: Immutable flags on world file
        immutable = False
        try:
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}', 'lsattr /var/lib/portage/world 2>/dev/null'],
                capture_output=True, text=True, timeout=10)
            immutable = 'i' in result.stdout.split()[0] if result.stdout.strip() else False
        except Exception:
            pass
        checks.append({
            'name': 'immutable_flags',
            'passed': True,  # Not a blocker — we handle it
            'detail': 'World file is immutable (will chattr -i before write)' if immutable else 'World file is mutable',
            'immutable': immutable,
        })

        # Check 4: Get current @world from drone
        current_world = set()
        try:
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}', 'cat /var/lib/portage/world 2>/dev/null'],
                capture_output=True, text=True, timeout=10)
            current_world = {w.strip() for w in result.stdout.strip().split('\n') if w.strip()}
        except Exception:
            pass

        # Check 5: Compute proposed @world (with critical guarantees)
        proposed = cp.db.get_allowlist_with_critical(drone_name) if cp.db else set()
        checks.append({
            'name': 'allowlist_sanity',
            'passed': len(proposed) >= len(CRITICAL_PACKAGES),
            'detail': f'{len(proposed)} packages in proposed @world ({len(CRITICAL_PACKAGES)} critical guaranteed)',
        })

        # Check 6: Critical packages status
        critical_status = []
        for pkg in sorted(CRITICAL_PACKAGES):
            critical_status.append({
                'package': pkg,
                'in_current': pkg in current_world,
                'in_proposed': pkg in proposed,
            })
        all_critical_present = all(c['in_proposed'] for c in critical_status)
        checks.append({
            'name': 'critical_packages',
            'passed': all_critical_present,
            'detail': f'All {len(CRITICAL_PACKAGES)} critical packages present in proposed @world',
            'packages': critical_status,
        })

        # Compute diff
        removing = sorted(current_world - proposed)
        adding = sorted(proposed - current_world)
        keeping = sorted(current_world & proposed)

        diff = {
            'current_count': len(current_world),
            'proposed_count': len(proposed),
            'removing': removing,
            'removing_count': len(removing),
            'adding': adding,
            'adding_count': len(adding),
            'keeping': keeping,
            'keeping_count': len(keeping),
        }

        # All checks must pass
        all_passed = all(c['passed'] for c in checks)

        if not all_passed:
            self.send_json({
                'status': 'preflight_failed',
                'drone': drone_name,
                'checks': checks,
                'diff': diff,
                'error': 'One or more pre-flight checks failed',
            })
            return

        # Issue one-time token (5-minute expiry)
        token = secrets.token_hex(24)
        _preflight_tokens[token] = {
            'drone': drone_name,
            'ip': ip,
            'expires': time.time() + 300,
            'diff': diff,
            'proposed': sorted(proposed),
            'immutable': immutable,
        }

        # Prune expired tokens
        now = time.time()
        expired = [t for t, v in _preflight_tokens.items() if v['expires'] < now]
        for t in expired:
            del _preflight_tokens[t]

        self.send_json({
            'status': 'preflight_ok',
            'drone': drone_name,
            'checks': checks,
            'diff': diff,
            'critical_packages': critical_status,
            'preflight_token': token,
            'expires_in_s': 300,
            'confirm_instructions': f'To execute, POST to /clean/execute with preflight_token and confirm_name="{drone_name}"',
        })

    def _handle_clean_execute(self, cp, drone_name: str, body: dict):
        """Phase 2 of clean: validate token, write @world, verify, depclean.

        Requires:
          - preflight_token: from Phase 1 (valid, unexpired, matching drone)
          - confirm_name: must exactly match drone_name
        """
        import subprocess
        from .events import add_event

        token = body.get('preflight_token')
        confirm_name = body.get('confirm_name')

        # Validate token
        if not token or token not in _preflight_tokens:
            self.send_error_json(403, 'Invalid or expired preflight token. Run preflight again.')
            return

        token_data = _preflight_tokens[token]
        if token_data['drone'] != drone_name:
            self.send_error_json(403, f'Token was issued for {token_data["drone"]}, not {drone_name}')
            return

        if time.time() > token_data['expires']:
            del _preflight_tokens[token]
            self.send_error_json(403, 'Preflight token expired (5-minute limit). Run preflight again.')
            return

        # Validate typed confirmation
        if confirm_name != drone_name:
            self.send_error_json(400,
                f'Confirmation mismatch: you typed "{confirm_name}" but the drone is "{drone_name}". '
                f'Type the exact drone name to confirm.')
            return

        # Consume the token (one-time use)
        del _preflight_tokens[token]

        ip = token_data['ip']
        proposed = token_data['proposed']
        immutable = token_data['immutable']
        world_content = '\n'.join(proposed)

        # Re-check: drone not building (race condition guard)
        node = cp.db.get_node_by_name(drone_name) if cp.db else None
        if node:
            current_task = node.get('current_task')
            delegated = cp.db.get_delegated_packages(node['id']) if cp.db else []
            is_building = bool(current_task) or any(p.get('building_since') for p in delegated)
            if is_building:
                self.send_error_json(409,
                    f'Drone started building since preflight ({current_task}). Aborting clean.')
                return

        steps = []

        try:
            # Step 1: Remove immutable flag if needed
            if immutable:
                result = subprocess.run(
                    ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                     f'root@{ip}', 'chattr -i /var/lib/portage/world'],
                    capture_output=True, text=True, timeout=10)
                if result.returncode != 0:
                    self.send_error_json(500,
                        f'Failed to remove immutable flag: {result.stderr.strip()}')
                    return
                steps.append('Removed immutable flag from world file')

            # Step 2: Write @world via stdin (safe — no shell escaping issues)
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}', 'cat > /var/lib/portage/world'],
                input=world_content + '\n',
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                self.send_error_json(500,
                    f'Failed to write world file: {result.stderr.strip()}')
                return
            steps.append(f'Wrote {len(proposed)} packages to @world')

            # Step 3: Verify the write
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}', 'wc -l < /var/lib/portage/world'],
                capture_output=True, text=True, timeout=10)
            written_count = int(result.stdout.strip()) if result.stdout.strip().isdigit() else -1
            if written_count != len(proposed):
                self.send_error_json(500,
                    f'Write verification failed: expected {len(proposed)} lines, got {written_count}')
                return
            steps.append(f'Verified: {written_count} lines written')

            # Step 4: Restore immutable flag
            if immutable:
                subprocess.run(
                    ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                     f'root@{ip}', 'chattr +i /var/lib/portage/world'],
                    capture_output=True, text=True, timeout=10)
                steps.append('Restored immutable flag on world file')

            # Step 5: Switch profile to base
            result = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'eselect profile set default/linux/amd64/23.0 2>&1'],
                capture_output=True, text=True, timeout=15)
            steps.append(f'Profile switch: {result.stdout.strip() or "done"}')

            # Step 6: Start depclean in background
            subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 f'root@{ip}',
                 'nohup emerge --depclean --ask=n > /tmp/depclean.log 2>&1 &'],
                capture_output=True, text=True, timeout=15)
            steps.append('Started depclean in background (check /tmp/depclean.log on drone)')

            # Log event
            add_event('clean', f'Cleaned {drone_name}: wrote {len(proposed)} packages to @world',
                      details={
                          'drone': drone_name,
                          'removed': token_data['diff']['removing'],
                          'added': token_data['diff']['adding'],
                          'proposed_count': len(proposed),
                      })

            self.send_json({
                'status': 'ok',
                'drone': drone_name,
                'steps': steps,
                'diff': token_data['diff'],
                'world_packages': proposed,
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
