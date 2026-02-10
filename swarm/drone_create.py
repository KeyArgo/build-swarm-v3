"""
Drone creation orchestrator for Build Swarm v3.

Guides the user through creating a VM/container and bootstrapping it
as a build drone. Supports interactive and non-interactive modes.

Usage:
    build-swarmv3 drone create                          # Interactive wizard
    build-swarmv3 drone create --backend docker --name drone-05
    build-swarmv3 drone create --list-backends
"""

import os
import sys
import time
from typing import Dict, List, Optional

from swarm.backends import (
    get_backend, detect_available_backends, BackendError, BACKENDS, StepResult
)
from swarm.backends.stage3 import get_cache_dir


# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_CORES = 4
DEFAULT_RAM_MB = 4096
DEFAULT_DISK_GB = 50

KNOWN_PROXMOX_HOSTS = {
    'proxmox-io': '10.0.0.2',
    'proxmox-titan': '10.0.0.3',
}

SSH_PUBKEY_PATHS = [
    os.path.expanduser('~/.ssh/id_ed25519.pub'),
    os.path.expanduser('~/.ssh/id_rsa.pub'),
    '/root/.ssh/id_ed25519.pub',
    '/root/.ssh/id_rsa.pub',
]


# ── SSH Key Discovery ────────────────────────────────────────────────────

def find_ssh_pubkey():
    # type: () -> Optional[str]
    """Find an SSH public key to inject into the new VM/container."""
    for path in SSH_PUBKEY_PATHS:
        if os.path.isfile(path):
            with open(path) as f:
                key = f.read().strip()
                if key.startswith('ssh-'):
                    return key
    return None


# ── Display Helpers ──────────────────────────────────────────────────────

def _get_colors():
    """Import colors from CLI module."""
    try:
        from swarm.cli import C
        return C
    except ImportError:
        # Fallback minimal colors
        class C:
            RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = ''
            MAGENTA = CYAN = WHITE = BRED = BGREEN = BYELLOW = ''
            BBLUE = BMAGENTA = BCYAN = ''
        return C


def _print_step(current, total, label):
    # type: (int, int, str) -> None
    C = _get_colors()
    if total > 0:
        print('  {}[{}/{}]{} {}... '.format(
            C.BOLD, current, total, C.RESET, label), end='', flush=True)
    else:
        print('  {}{}...{} '.format(C.DIM, label, C.RESET),
              end='', flush=True)


def _print_ok(message):
    # type: (str) -> None
    C = _get_colors()
    print('{}OK{}  {}{}{}'.format(C.BGREEN, C.RESET, C.DIM, message, C.RESET))


def _print_fail(message):
    # type: (str) -> None
    C = _get_colors()
    print('{}FAILED{}  {}'.format(C.RED, C.RESET, message))


def _print_dry_run(backend):
    C = _get_colors()
    print('\n{}{}Dry Run{} -- the following steps would be taken:\n'.format(
        C.BOLD, C.YELLOW, C.RESET))
    for step in backend.dry_run_summary():
        print('  {}{}{}'.format(C.DIM, step, C.RESET))
    print()


# ── Interactive Wizard ───────────────────────────────────────────────────

def _input_with_default(prompt, default=''):
    # type: (str, str) -> str
    """Read input with a default value shown in brackets."""
    if default:
        raw = input('  {} [{}]: '.format(prompt, default)).strip()
    else:
        raw = input('  {}: '.format(prompt)).strip()
    return raw if raw else default


def interactive_create():
    # type: () -> Dict
    """Guided interactive mode for drone creation.

    Asks the user step-by-step questions with sensible defaults.
    Returns a dict of options compatible with create_drone() kwargs.
    """
    C = _get_colors()

    print('\n{}{}=== Drone Creation Wizard ==={}\n'.format(
        C.BOLD, C.BCYAN, C.RESET))
    print('  {}This wizard will guide you through creating a new build drone.{}'.format(
        C.DIM, C.RESET))
    print('  {}Press Enter to accept [defaults] shown in brackets.{}\n'.format(
        C.DIM, C.RESET))

    # Step 1: Backend selection
    available = detect_available_backends()
    print('  {}Available backends:{}'.format(C.BOLD, C.RESET))
    ready_backends = []
    for i, (name, status, desc) in enumerate(available, 1):
        if status == 'available':
            sc = C.BGREEN
            marker = 'ready'
            ready_backends.append(name)
        else:
            sc = C.RED
            marker = 'unavailable'
        print('    {}{}.{} {:<20} {}{:<12}{} {}{}{}'.format(
            C.CYAN, i, C.RESET,
            name, sc, marker, C.RESET,
            C.DIM, desc, C.RESET))

    if not ready_backends:
        print('\n  {}No backends available!{}'.format(C.RED, C.RESET))
        print('  {}Install Docker, virsh, or set up SSH to a Proxmox host.{}'.format(
            C.DIM, C.RESET))
        sys.exit(1)

    print()
    choice = _input_with_default('Backend (number or name)', '1')

    # Parse choice
    selected_backend = None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(available):
            selected_backend = available[idx][0]
    except ValueError:
        if choice in [b[0] for b in available]:
            selected_backend = choice

    if selected_backend is None:
        print('  {}Invalid choice: {}{}'.format(C.RED, choice, C.RESET))
        sys.exit(1)

    if selected_backend not in ready_backends:
        print('  {}Backend "{}" is not available.{}'.format(
            C.YELLOW, selected_backend, C.RESET))
        sys.exit(1)

    # Step 2: Host selection (Proxmox backends only)
    host = None
    if selected_backend in ('proxmox-lxc', 'proxmox-qemu'):
        print('\n  {}Proxmox hosts:{}'.format(C.BOLD, C.RESET))
        hosts = list(KNOWN_PROXMOX_HOSTS.items())
        for i, (label, ip) in enumerate(hosts, 1):
            print('    {}{}.{} {}  ({})'.format(C.CYAN, i, C.RESET, label, ip))
        print()
        h_choice = _input_with_default('Proxmox host (number, name, or IP)', '1')

        try:
            h_idx = int(h_choice) - 1
            if 0 <= h_idx < len(hosts):
                host = hosts[h_idx][1]
        except ValueError:
            if h_choice in KNOWN_PROXMOX_HOSTS:
                host = KNOWN_PROXMOX_HOSTS[h_choice]
            else:
                host = h_choice  # Assume it's an IP

    # Step 3: Name
    print()
    name = _input_with_default('Drone name', _auto_drone_name())

    # Step 4: Resources
    print()
    cores_str = _input_with_default('CPU cores', str(DEFAULT_CORES))
    ram_str = _input_with_default('RAM in MB', str(DEFAULT_RAM_MB))
    disk_str = _input_with_default('Disk in GB', str(DEFAULT_DISK_GB))

    try:
        cores = int(cores_str)
        ram_mb = int(ram_str)
        disk_gb = int(disk_str)
    except ValueError:
        print('  {}Invalid number entered.{}'.format(C.RED, C.RESET))
        sys.exit(1)

    # Step 5: IP
    print()
    ip_input = _input_with_default('IP address (blank for DHCP)', '')
    ip = ip_input if ip_input else None

    # Step 6: SSH key
    ssh_pubkey = find_ssh_pubkey()
    if ssh_pubkey:
        key_short = ssh_pubkey.split()[-1] if ' ' in ssh_pubkey else ssh_pubkey[:40]
    else:
        key_short = 'NONE FOUND'

    # Step 7: Summary
    print('\n  {}=== Summary ==={}'.format(C.BOLD, C.RESET))
    print('    {}Backend:{}   {}{}{}'.format(C.DIM, C.RESET, C.CYAN, selected_backend, C.RESET))
    if host:
        print('    {}Host:{}      {}'.format(C.DIM, C.RESET, host))
    print('    {}Name:{}      {}'.format(C.DIM, C.RESET, name))
    print('    {}Resources:{} {} cores, {}MB RAM, {}GB disk'.format(
        C.DIM, C.RESET, cores, ram_mb, disk_gb))
    print('    {}Network:{}   {}'.format(C.DIM, C.RESET, ip or 'DHCP'))
    print('    {}SSH key:{}   {}'.format(C.DIM, C.RESET, key_short))
    print()

    confirm = input('  Proceed? [Y/n]: ').strip().lower()
    if confirm and confirm not in ('y', 'yes'):
        print('\n  {}Aborted.{}'.format(C.DIM, C.RESET))
        sys.exit(0)

    return {
        'backend': selected_backend,
        'host': host,
        'name': name,
        'cores': cores,
        'ram_mb': ram_mb,
        'disk_gb': disk_gb,
        'ip': ip,
        'ssh_pubkey': ssh_pubkey,
    }


def _auto_drone_name():
    # type: () -> str
    """Generate a default drone name like drone-05."""
    # Try to discover existing drones to pick the next number
    try:
        from swarm.drone_audit import discover_drones
        known = discover_drones()
        numbers = []
        for name in known:
            if name.startswith('drone-'):
                try:
                    numbers.append(int(name.split('-')[1]))
                except (ValueError, IndexError):
                    pass
        if numbers:
            return 'drone-{:02d}'.format(max(numbers) + 1)
    except Exception:
        pass
    return 'drone-new'


# ── List Backends ────────────────────────────────────────────────────────

def list_backends():
    """Print available backends and their status."""
    C = _get_colors()
    available = detect_available_backends()

    print('\n{}{}=== Available Backends ==={}\n'.format(
        C.BOLD, C.BCYAN, C.RESET))

    for name, status, desc in available:
        if status == 'available':
            sc = C.BGREEN
        else:
            sc = C.RED
        print('  {}{:<20}{} {}{:<12}{} {}'.format(
            C.CYAN, name, C.RESET,
            sc, status, C.RESET, desc))

    print()
    print('  {}Use: build-swarmv3 drone create --backend <name> --name <drone-name>{}'.format(
        C.DIM, C.RESET))
    print('  {}  Or: build-swarmv3 drone create  (for interactive wizard){}'.format(
        C.DIM, C.RESET))
    print()


# ── Main Create Orchestrator ─────────────────────────────────────────────

def create_drone(backend,       # type: str
                 name,          # type: str
                 host=None,     # type: Optional[str]
                 ip=None,       # type: Optional[str]
                 cores=DEFAULT_CORES,       # type: int
                 ram_mb=DEFAULT_RAM_MB,     # type: int
                 disk_gb=DEFAULT_DISK_GB,   # type: int
                 vmid=None,     # type: Optional[int]
                 storage='local-lvm',   # type: str
                 bridge='vmbr0',        # type: str
                 ssh_pubkey=None,       # type: Optional[str]
                 dry_run=False,         # type: bool
                 skip_deploy=False,     # type: bool
                 cp_url=None,           # type: Optional[str]
                 ):
    # type: (...) -> Dict
    """Create a VM/container and bootstrap it as a build drone.

    This is the main entry point. It:
    1. Instantiates the appropriate backend
    2. Runs through all lifecycle steps with progress display
    3. If all steps succeed, runs drone deploy (bootstrap.sh)
    4. Reports final status

    Returns a result dict with status, IP, name, backend details.
    """
    C = _get_colors()

    # Find SSH pubkey if not provided
    if not ssh_pubkey:
        ssh_pubkey = find_ssh_pubkey()
        if not ssh_pubkey:
            return {
                'status': 'error',
                'error': 'No SSH public key found. '
                         'Create one with: ssh-keygen -t ed25519',
            }

    # Instantiate backend
    try:
        be = get_backend(
            backend,
            host=host, name=name, vmid=vmid,
            ip=ip, cores=cores, ram_mb=ram_mb,
            disk_gb=disk_gb, storage=storage,
            bridge=bridge, ssh_pubkey=ssh_pubkey,
        )
    except BackendError as e:
        return {'status': 'error', 'error': str(e)}

    # Dry run mode
    if dry_run:
        _print_dry_run(be)
        return {'status': 'dry_run', 'steps': be.dry_run_summary()}

    cache_dir = get_cache_dir()

    # Execute lifecycle steps
    steps = [
        ('Checking prerequisites', be.check_prerequisites),
        ('Allocating ID', be.allocate_id),
        ('Preparing base image', lambda: be.download_image(cache_dir)),
        ('Creating VM/container', be.create),
        ('Configuring network', be.configure_network),
        ('Injecting SSH key', lambda: be.inject_ssh_key(ssh_pubkey)),
        ('Starting VM/container', be.start),
        ('Waiting for SSH', lambda: be.wait_for_ssh(timeout=180)),
    ]

    total_steps = len(steps) + (0 if skip_deploy else 1)
    completed = 0

    for label, step_fn in steps:
        completed += 1
        _print_step(completed, total_steps, label)

        try:
            result = step_fn()
        except Exception as e:
            _print_fail(str(e))
            print()
            _print_step(0, 0, 'Cleaning up')
            try:
                be.cleanup_on_failure()
                _print_ok('Resources cleaned up')
            except Exception:
                _print_fail('Cleanup also failed')
            return {
                'status': 'error',
                'error': 'Step "{}" failed: {}'.format(label, e),
                'step': label,
            }

        if not result.ok:
            _print_fail(result.message)
            if result.detail:
                print('    {}{}{}'.format(C.DIM, result.detail, C.RESET))
            print()
            _print_step(0, 0, 'Cleaning up')
            try:
                cleanup = be.cleanup_on_failure()
                if cleanup.ok:
                    _print_ok(cleanup.message)
                else:
                    _print_fail(cleanup.message)
            except Exception:
                _print_fail('Cleanup failed')
            return {
                'status': 'error',
                'error': result.message,
                'detail': result.detail,
                'step': label,
            }

        _print_ok(result.message)

    drone_ip = be.get_ip()

    # Run drone deploy pipeline (bootstrap.sh)
    if not skip_deploy:
        completed += 1
        _print_step(completed, total_steps, 'Running drone bootstrap')

        cp_url = cp_url or os.environ.get(
            'SWARMV3_URL', 'http://10.0.0.199:8100')

        try:
            from swarm.drone_audit import deploy_drone_ssh
            deploy_result = deploy_drone_ssh(
                ip=drone_ip,
                cp_url=cp_url,
                name=name,
                prune=False,
                dry_run=False,
                timeout=900,
            )
        except Exception as e:
            _print_fail(str(e))
            return {
                'status': 'partial',
                'error': 'VM created but bootstrap failed: {}'.format(e),
                'ip': drone_ip,
                'name': name,
                'backend': backend,
            }

        if deploy_result.get('status') == 'success':
            _print_ok('Bootstrap complete')
        else:
            _print_fail('Bootstrap failed: {}'.format(
                deploy_result.get('error', '')))
            return {
                'status': 'partial',
                'error': 'VM created but bootstrap failed',
                'ip': drone_ip,
                'name': name,
                'backend': backend,
                'deploy_error': deploy_result.get('error'),
            }

    # Success!
    return {
        'status': 'success',
        'ip': drone_ip,
        'name': name,
        'backend': backend,
        'host': host,
        'vmid': getattr(be, 'vmid', None),
    }
