"""
Configuration loading for Build Swarm v3.

Reads existing swarm.json for backward compatibility with v2 node definitions.
All runtime config is stored in SQLite config table.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

log = logging.getLogger('swarm-v3')

# Default paths — portable: XDG for non-root, system paths for root
if hasattr(os, 'getuid') and os.getuid() == 0:
    _DATA_DIR = '/var/lib/build-swarm-v3'
    _LOG_DIR = '/var/log/build-swarm-v3'
else:
    _DATA_DIR = os.path.join(
        os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')),
        'build-swarm-v3')
    _LOG_DIR = os.path.join(
        os.environ.get('XDG_STATE_HOME', os.path.expanduser('~/.local/state')),
        'build-swarm-v3')

# v2 compatibility — set V2_SWARM_CONFIG to point at your v2 swarm.json
_v2_default = os.path.expanduser('~/Development/gentoo-build-swarm/config/swarm.json')
_v2_env = os.environ.get('V2_SWARM_CONFIG', '')
V2_SWARM_CONFIG = Path(_v2_env) if _v2_env else (Path(_v2_default) if Path(_v2_default).exists() else None)

# v3 settings
CONTROL_PLANE_PORT = int(os.environ.get('CONTROL_PLANE_PORT', 8100))
DB_PATH = os.environ.get('SWARM_DB_PATH', os.path.join(_DATA_DIR, 'swarm.db'))
LOG_FILE = os.environ.get('LOG_FILE', os.path.join(_LOG_DIR, 'control-plane.log'))

# Build behavior (same defaults as v2 orchestrator)
MAX_DRONE_FAILURES = int(os.environ.get('MAX_DRONE_FAILURES', 8))
GROUNDING_TIMEOUT_MINUTES = int(os.environ.get('GROUNDING_TIMEOUT', 5))
FAILURE_AGE_MINUTES = int(os.environ.get('FAILURE_AGE_MINUTES', 30))
QUEUE_TARGET = int(os.environ.get('QUEUE_TARGET', 5))
CORES_PER_SLOT = int(os.environ.get('CORES_PER_SLOT', 4))
NODE_TIMEOUT = int(os.environ.get('NODE_TIMEOUT', 30))
STALE_TIMEOUT = int(os.environ.get('STALE_TIMEOUT', 300))

# Sweeper configuration
SWEEPER_PREFIX = os.environ.get('SWEEPER_PREFIX', 'sweeper-')
SWEEPER_THRESHOLD = int(os.environ.get('SWEEPER_THRESHOLD', MAX_DRONE_FAILURES))

# Upload failure circuit breaker (network-aware scheduling)
MAX_UPLOAD_FAILURES = int(os.environ.get('MAX_UPLOAD_FAILURES', 3))
UPLOAD_RETRY_INTERVAL_M = int(os.environ.get('UPLOAD_RETRY_INTERVAL_M', 30))

# Staging paths (separate from v2)
STAGING_PATH = os.environ.get('STAGING_PATH', '/var/cache/binpkgs-v3-staging')
BINHOST_PATH = os.environ.get('BINHOST_PATH', '/var/cache/binpkgs-v3')

# Admin dashboard
ADMIN_PORT = int(os.environ.get('ADMIN_PORT', 8093))
ADMIN_SECRET = os.environ.get('ADMIN_SECRET', '')
ADMIN_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'admin')

# Binhost configuration (primary = seasonal, secondary = always-on)
BINHOST_PRIMARY_IP = os.environ.get('BINHOST_PRIMARY_IP', '10.0.0.199')
BINHOST_SECONDARY_IP = os.environ.get('BINHOST_SECONDARY_IP', '100.114.16.118')
BINHOST_PRIMARY_PATH = os.environ.get('BINHOST_PRIMARY_PATH', '/var/cache/binpkgs')
BINHOST_SECONDARY_PATH = os.environ.get('BINHOST_SECONDARY_PATH', '/var/cache/binpkgs')
BINHOST_PRIMARY_PORT = int(os.environ.get('BINHOST_PRIMARY_PORT', 80))
BINHOST_SECONDARY_PORT = int(os.environ.get('BINHOST_SECONDARY_PORT', 80))

# V2 gateway proxy
V2_GATEWAY_URL = os.environ.get('V2_GATEWAY_URL', 'http://10.0.0.199:8090')

# Control plane URL — auto-discover if not set
KNOWN_HOSTS = ['localhost', '10.0.0.199']

def discover_control_plane(port=None):
    """Find a running control plane. Checks localhost first, then known hosts."""
    import urllib.request
    port = port or CONTROL_PLANE_PORT
    env_url = os.environ.get('SWARMV3_URL')
    if env_url:
        return env_url
    for host in KNOWN_HOSTS:
        url = f'http://{host}:{port}'
        try:
            req = urllib.request.Request(f'{url}/api/v1/health', method='GET')
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            continue
    return f'http://localhost:{port}'

# Protected hosts
def _load_protected_hosts() -> set:
    hosts = set()
    env_hosts = os.environ.get('PROTECTED_HOSTS', '')
    if env_hosts:
        hosts.update(ip.strip() for ip in env_hosts.split(',') if ip.strip())
    config_file = Path('/etc/build-swarm/protected_hosts.conf')
    if config_file.exists():
        try:
            for line in config_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    hosts.add(line)
        except Exception:
            pass
    return hosts

PROTECTED_HOSTS = _load_protected_hosts()


def load_v2_config() -> Dict[str, Any]:
    """Load existing swarm.json for node definitions and portage config."""
    if V2_SWARM_CONFIG is None or not V2_SWARM_CONFIG.exists():
        if V2_SWARM_CONFIG is not None:
            log.warning(f"v2 config not found at {V2_SWARM_CONFIG}")
        return {}

    try:
        data = json.loads(V2_SWARM_CONFIG.read_text())
        log.info(f"Loaded v2 config from {V2_SWARM_CONFIG}")
        return data
    except Exception as e:
        log.error(f"Failed to load v2 config: {e}")
        return {}


def get_portage_config(v2_config: Dict = None) -> Dict[str, Any]:
    """Get drone portage configuration (from v2 config)."""
    if v2_config is None:
        v2_config = load_v2_config()
    return v2_config.get('drone_portage_config', {})


def get_package_exclusions(v2_config: Dict = None) -> list:
    """Get package exclusion list."""
    if v2_config is None:
        v2_config = load_v2_config()
    return v2_config.get('package_exclusions', {}).get('packages', [])


def get_sweeper_packages(v2_config: Dict = None) -> Dict[str, Any]:
    """Get sweeper-specific package assignments."""
    if v2_config is None:
        v2_config = load_v2_config()
    return v2_config.get('sweeper_packages', {})


def setup_logging(name: str = 'swarm-v3', log_file: str = None):
    """Configure logging for v3 components."""
    log_file = log_file or LOG_FILE

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # File handler
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(fh)
    except Exception:
        pass  # Skip file logging if directory isn't writable

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    ))
    logger.addHandler(ch)

    return logger
