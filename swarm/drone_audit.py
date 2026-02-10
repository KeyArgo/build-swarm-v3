"""
Drone audit and deployment for Build Swarm v3.

Provides SSH-based compliance checking and bootstrap deployment for drones.
Uses drone-image/ spec files as the source of truth.
"""

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger('swarm-v3')

# ── Spec loading ─────────────────────────────────────────────────────────────

def _find_drone_image_dir() -> str:
    """Locate the drone-image/ directory relative to the package."""
    # Check relative to this file (in-tree development)
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(pkg_dir, 'drone-image')
    if os.path.isdir(candidate):
        return candidate

    # Check installed package data
    try:
        import importlib.resources as resources
        ref = resources.files('swarm').joinpath('drone-image')
        if ref.is_dir():
            return str(ref)
    except Exception:
        pass

    return candidate  # Return the development path as default


def load_spec(spec_path: str = None) -> dict:
    """Load the drone spec JSON.

    Search order:
    1. Explicit spec_path argument
    2. drone-image/drone.spec relative to the package
    3. /etc/build-swarm/drone.spec (on a drone itself)
    """
    candidates = []
    if spec_path:
        candidates.append(spec_path)
    candidates.append(os.path.join(_find_drone_image_dir(), 'drone.spec'))
    candidates.append('/etc/build-swarm/drone.spec')

    for path in candidates:
        if os.path.isfile(path):
            with open(path) as f:
                return json.load(f)

    raise FileNotFoundError(
        f'No drone spec found. Searched: {", ".join(candidates)}')


def load_file(name: str) -> str:
    """Load a file from the drone-image/ directory."""
    path = os.path.join(_find_drone_image_dir(), name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f'Not found: {path}')
    with open(path) as f:
        return f.read()


# ── SSH helpers ──────────────────────────────────────────────────────────────

def _ssh_run(ip: str, command: str, timeout: int = 60,
             stdin_data: str = None) -> subprocess.CompletedProcess:
    """Run a command on a remote host via SSH."""
    ssh_cmd = [
        'ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        f'root@{ip}', command
    ]
    return subprocess.run(
        ssh_cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ssh_pipe(ip: str, script: str, args: str = '',
              timeout: int = 600) -> subprocess.CompletedProcess:
    """Pipe a script to bash on a remote host via SSH."""
    ssh_cmd = [
        'ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        f'root@{ip}', f'bash -s -- {args}'
    ]
    return subprocess.run(
        ssh_cmd,
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Audit ────────────────────────────────────────────────────────────────────

def audit_drone_ssh(ip: str, spec: dict = None,
                    timeout: int = 60) -> dict:
    """SSH to a drone and run the compliance checker.

    Base64-encodes the spec and comply.sh, sends them via SSH,
    decodes on the remote side, and runs the check.
    Returns a structured result dict.
    """
    import base64
    import re

    if spec is None:
        spec = load_spec()

    spec_json = json.dumps(spec)

    try:
        comply_script = load_file('comply.sh')
    except FileNotFoundError:
        return {
            'ip': ip,
            'status': 'error',
            'error': 'comply.sh not found in drone-image/',
            'checks': [],
        }

    # Base64-encode both files to avoid heredoc nesting issues
    spec_b64 = base64.b64encode(spec_json.encode()).decode()
    comply_b64 = base64.b64encode(comply_script.encode()).decode()

    remote_cmd = (
        f"TMPSPEC=$(mktemp) && TMPSCRIPT=$(mktemp) && "
        f"trap 'rm -f $TMPSPEC $TMPSCRIPT' EXIT && "
        f"echo '{spec_b64}' | base64 -d > $TMPSPEC && "
        f"echo '{comply_b64}' | base64 -d > $TMPSCRIPT && "
        f"bash $TMPSCRIPT --spec $TMPSPEC"
    )

    try:
        result = _ssh_run(ip, remote_cmd, timeout)
    except subprocess.TimeoutExpired:
        return {
            'ip': ip,
            'status': 'timeout',
            'error': f'SSH timed out after {timeout}s',
            'checks': [],
        }
    except FileNotFoundError:
        return {
            'ip': ip,
            'status': 'error',
            'error': 'ssh command not found',
            'checks': [],
        }
    except Exception as e:
        return {
            'ip': ip,
            'status': 'error',
            'error': str(e),
            'checks': [],
        }

    # Parse comply.sh output
    checks = []
    summary = {}
    raw_output = result.stdout

    for line in raw_output.splitlines():
        line_stripped = line.strip()
        # Skip empty lines and the header
        if not line_stripped:
            continue
        if '===' in line_stripped:
            continue
        if line_stripped.startswith('SUMMARY:'):
            # Parse "SUMMARY: 5 PASS, 1 WARN, 2 FAIL  (8 checks)"
            summary['raw'] = line_stripped
            continue

        # Parse PASS/WARN/FAIL lines (stripping ANSI codes)
        clean = re.sub(r'\033\[[0-9;]*m', '', line_stripped)

        for status_prefix in ('PASS', 'WARN', 'FAIL'):
            if clean.startswith(status_prefix):
                rest = clean[len(status_prefix):].strip()
                # Split into check name and detail
                parts = rest.split(None, 1)
                check_name = parts[0] if parts else 'unknown'
                detail = parts[1] if len(parts) > 1 else ''
                checks.append({
                    'status': status_prefix.lower(),
                    'check': check_name,
                    'detail': detail,
                })
                break

    # Determine overall status from exit code
    if result.returncode == 0:
        overall = 'compliant'
    elif result.returncode == 1:
        overall = 'warnings'
    else:
        overall = 'non-compliant'

    return {
        'ip': ip,
        'status': overall,
        'exit_code': result.returncode,
        'checks': checks,
        'raw_output': raw_output,
        'raw_stderr': result.stderr,
        'pass': sum(1 for c in checks if c['status'] == 'pass'),
        'warn': sum(1 for c in checks if c['status'] == 'warn'),
        'fail': sum(1 for c in checks if c['status'] == 'fail'),
    }


# ── Deploy ───────────────────────────────────────────────────────────────────

def build_bootstrap_script(cp_url: str, name: str = None,
                           prune: bool = False) -> str:
    """Build a self-contained bootstrap script with embedded config files.

    Reads the template from drone-image/bootstrap.sh and substitutes
    the placeholders with actual file contents.
    """
    script = load_file('bootstrap.sh')

    # Read the config files to embed
    substitutions = {
        '__MAKE_CONF__': load_file('make.conf.drone'),
        '__PACKAGE_USE__': load_file('package.use.drone'),
        '__PACKAGE_KEYWORDS__': load_file('package.accept_keywords.drone'),
    }

    # For package list, strip comments and empty lines
    pkg_list_raw = load_file('package.list')
    pkg_atoms = '\n'.join(
        line for line in pkg_list_raw.splitlines()
        if line.strip() and not line.strip().startswith('#')
    )
    substitutions['__PACKAGE_LIST__'] = pkg_atoms

    for placeholder, content in substitutions.items():
        script = script.replace(placeholder, content)

    return script


def deploy_drone_ssh(ip: str, cp_url: str, name: str = None,
                     prune: bool = False, dry_run: bool = False,
                     timeout: int = 600) -> dict:
    """Deploy a drone by piping bootstrap.sh via SSH.

    Returns a result dict with status and output.
    """
    # Build args for bootstrap.sh
    args_parts = [f'--cp-url {cp_url}']
    if name:
        args_parts.append(f'--name {name}')
    if prune:
        args_parts.append('--prune')
    if dry_run:
        args_parts.append('--dry-run')
    args_str = ' '.join(args_parts)

    try:
        script = build_bootstrap_script(cp_url, name, prune)
    except FileNotFoundError as e:
        return {
            'ip': ip,
            'status': 'error',
            'error': f'Missing template file: {e}',
        }

    # Test SSH first
    try:
        test = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
             f'root@{ip}', 'echo ok'],
            capture_output=True, text=True, timeout=10)
        if test.returncode != 0:
            return {
                'ip': ip,
                'status': 'ssh_failed',
                'error': f'SSH test failed: {test.stderr.strip()}',
            }
    except subprocess.TimeoutExpired:
        return {
            'ip': ip,
            'status': 'ssh_timeout',
            'error': f'SSH connection to {ip} timed out',
        }
    except FileNotFoundError:
        return {
            'ip': ip,
            'status': 'error',
            'error': 'ssh command not found',
        }

    # Run bootstrap
    try:
        result = _ssh_pipe(ip, script, args_str, timeout)
    except subprocess.TimeoutExpired:
        return {
            'ip': ip,
            'status': 'timeout',
            'error': f'Bootstrap timed out after {timeout}s',
        }
    except Exception as e:
        return {
            'ip': ip,
            'status': 'error',
            'error': str(e),
        }

    return {
        'ip': ip,
        'name': name,
        'status': 'success' if result.returncode == 0 else 'failed',
        'exit_code': result.returncode,
        'output': result.stdout,
        'errors': result.stderr,
    }


# ── Drone discovery ─────────────────────────────────────────────────────────

def discover_drones(gateway_host: str = '10.0.0.199',
                    v2_port: str = '8090',
                    v3_port: str = '8100') -> dict:
    """Discover drones from both v2 and v3 control plane APIs.

    Returns {name: ip} dict.
    """
    import urllib.request
    known = {}

    for port, label in [(v2_port, 'v2'), (v3_port, 'v3')]:
        try:
            url = f'http://{gateway_host}:{port}/api/v1/nodes?all=true'
            req = urllib.request.Request(url)
            req.add_header('Accept', 'application/json')
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                for d in data.get('drones', []):
                    name = d.get('name', '')
                    ip = d.get('ip', '')
                    if name and ip:
                        known[name] = ip
        except Exception:
            pass

    return known
