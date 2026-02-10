"""
Drone provisioner for Build Swarm v3.

Generates bootstrap scripts and handles SSH-based drone provisioning.
"""

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger('swarm-v3')

# Path to the drone binary (if available locally for distribution)
DRONE_BINARY = os.environ.get(
    'DRONE_BINARY',
    os.path.expanduser('~/Development/gentoo-build-swarm/build-drone'))


def generate_bootstrap_script(control_plane_url: str,
                              drone_name: str = None) -> str:
    """Generate a self-contained bootstrap script from drone-image/ templates.

    Reads bootstrap.sh and embeds make.conf, package.use, package.accept_keywords,
    and package.list by replacing placeholders. Falls back to the inline defaults
    in bootstrap.sh if template files aren't found.

    Usage: curl http://cp:8100/api/v1/provision/bootstrap | bash -s -- --cp-url URL
    """
    try:
        from swarm.drone_audit import build_bootstrap_script
        return build_bootstrap_script(control_plane_url, drone_name)
    except Exception as e:
        log.warning(f"Could not build from templates ({e}), using fallback")
        # Fall back to reading the raw bootstrap.sh without substitutions
        drone_image_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'drone-image')
        bootstrap_path = os.path.join(drone_image_dir, 'bootstrap.sh')
        if os.path.isfile(bootstrap_path):
            with open(bootstrap_path) as f:
                return f.read()
        raise FileNotFoundError(
            f'bootstrap.sh not found at {bootstrap_path}') from e


def provision_drone_ssh(ip: str, control_plane_url: str,
                        name: str = None) -> dict:
    """Provision a drone via SSH in a background thread.

    Tests SSH connectivity, then pipes the bootstrap script to the remote host.
    Returns a status dict immediately; provisioning runs in background.
    """
    result = {
        'status': 'initiating',
        'ip': ip,
        'name': name,
        'steps': [],
    }

    # Test SSH connectivity first
    try:
        test = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
             f'root@{ip}', 'echo ok'],
            capture_output=True, text=True, timeout=10)
        if test.returncode != 0:
            result['status'] = 'ssh_failed'
            result['error'] = f'SSH test failed: {test.stderr.strip()}'
            return result
        result['steps'].append('ssh_test: ok')
    except subprocess.TimeoutExpired:
        result['status'] = 'ssh_timeout'
        result['error'] = f'SSH connection to {ip} timed out'
        return result
    except FileNotFoundError:
        result['status'] = 'ssh_not_found'
        result['error'] = 'ssh command not found'
        return result

    # Run provisioning in background
    def _do_provision():
        try:
            script = generate_bootstrap_script(control_plane_url, name)
            name_arg = name or ''
            proc = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=10',
                 f'root@{ip}', f'bash -s -- {name_arg}'],
                input=script, capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                log.info(f"Provisioned drone at {ip} ({name or 'auto'})")
            else:
                log.error(f"Provision failed for {ip}: {proc.stderr[:500]}")
        except Exception as e:
            log.error(f"Provision error for {ip}: {e}")

    thread = threading.Thread(target=_do_provision, daemon=True,
                              name=f'provision-{ip}')
    thread.start()

    result['status'] = 'provisioning'
    result['steps'].append('bootstrap_started')
    return result
