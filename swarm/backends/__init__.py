"""
Backend registry for drone VM/container creation.

Each backend implements DroneBackend with the same 10-step lifecycle.
Auto-detects available backends by probing local commands and SSH targets.
"""

import abc
import os
import subprocess
from typing import Dict, List, Optional, Tuple


class BackendError(Exception):
    """Raised when a backend operation fails."""
    pass


class StepResult:
    """Result of a single provisioning step."""

    def __init__(self, ok: bool, message: str, detail: str = ''):
        self.ok = ok
        self.message = message
        self.detail = detail

    @classmethod
    def success(cls, message: str, detail: str = ''):
        return cls(True, message, detail)

    @classmethod
    def fail(cls, message: str, detail: str = ''):
        return cls(False, message, detail)


class DroneBackend(abc.ABC):
    """Base class for all VM/container creation backends.

    Each backend implements the full lifecycle:
    1. check_prerequisites() - Verify tools/access available
    2. allocate_id()         - Get next available VM/container ID
    3. download_image()      - Get/cache Gentoo stage3 or template
    4. create()              - Create the VM/container
    5. configure_network()   - Set up networking
    6. inject_ssh_key()      - Install SSH authorized_keys
    7. start()               - Start the VM/container
    8. wait_for_ssh()        - Poll until SSH is reachable
    9. get_ip()              - Return the IP address
    10. cleanup_on_failure() - Destroy partially-created resources
    """

    BACKEND_NAME = ''   # type: str
    DESCRIPTION = ''    # type: str

    def __init__(self, **kwargs):
        # Accept and ignore extra kwargs so subclasses can pass through
        pass

    @abc.abstractmethod
    def check_prerequisites(self):
        # type: () -> StepResult
        """Verify everything needed is available."""
        ...

    @abc.abstractmethod
    def allocate_id(self):
        # type: () -> StepResult
        """Allocate a VM/container ID."""
        ...

    @abc.abstractmethod
    def download_image(self, cache_dir):
        # type: (str) -> StepResult
        """Ensure the base image is available."""
        ...

    @abc.abstractmethod
    def create(self):
        # type: () -> StepResult
        """Create the VM/container."""
        ...

    @abc.abstractmethod
    def configure_network(self):
        # type: () -> StepResult
        """Configure networking (DHCP or static)."""
        ...

    @abc.abstractmethod
    def inject_ssh_key(self, pubkey):
        # type: (str) -> StepResult
        """Inject SSH public key for root access."""
        ...

    @abc.abstractmethod
    def start(self):
        # type: () -> StepResult
        """Start the VM/container."""
        ...

    @abc.abstractmethod
    def wait_for_ssh(self, timeout=120):
        # type: (int) -> StepResult
        """Wait for SSH to become available."""
        ...

    @abc.abstractmethod
    def get_ip(self):
        # type: () -> str
        """Return the IP address of the created VM/container."""
        ...

    @abc.abstractmethod
    def cleanup_on_failure(self):
        # type: () -> StepResult
        """Destroy partially-created resources."""
        ...

    def dry_run_summary(self):
        # type: () -> List[str]
        """Return a list of actions that WOULD be taken."""
        return ['{}: no dry-run detail available'.format(self.BACKEND_NAME)]


# ── Registry ────────────────────────────────────────────────────────────

BACKENDS = {}  # type: Dict[str, type]


def register_backend(name):
    # type: (str) -> callable
    """Decorator to register a backend class."""
    def decorator(cls):
        BACKENDS[name] = cls
        cls.BACKEND_NAME = name
        return cls
    return decorator


def get_backend(backend_name, **kwargs):
    # type: (str, ...) -> DroneBackend
    """Instantiate a backend by name."""
    if backend_name not in BACKENDS:
        available = ', '.join(sorted(BACKENDS.keys()))
        raise BackendError(
            "Unknown backend '{}'. Available: {}".format(backend_name, available))
    return BACKENDS[backend_name](**kwargs)


def detect_available_backends():
    # type: () -> List[Tuple[str, str, str]]
    """Probe which backends are available on this system.

    Returns list of (name, status, description) tuples.
    Status is one of: 'available', 'unavailable', 'error'.
    """
    results = []
    for name in sorted(BACKENDS.keys()):
        cls = BACKENDS[name]
        try:
            status = cls.probe_availability()
            results.append((name, status, cls.DESCRIPTION))
        except Exception as e:
            results.append((name, 'error', str(e)))
    return results


# ── SSH helper (shared with drone_audit.py) ──────────────────────────────

def ssh_run(ip, command, timeout=60, stdin_data=None):
    # type: (str, str, int, Optional[str]) -> subprocess.CompletedProcess
    """Run a command on a remote host via SSH."""
    ssh_cmd = [
        'ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        'root@{}'.format(ip), command
    ]
    return subprocess.run(
        ssh_cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ssh_test(ip, timeout=10):
    # type: (str, int) -> bool
    """Test SSH connectivity to a host. Returns True if reachable."""
    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new',
             'root@{}'.format(ip), 'echo ok'],
            capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0 and 'ok' in result.stdout
    except Exception:
        return False


# ── Import backends to trigger registration ──────────────────────────────

from . import docker         # noqa: E402, F401
from . import proxmox_lxc    # noqa: E402, F401
from . import proxmox_qemu   # noqa: E402, F401
from . import qemu_local     # noqa: E402, F401
