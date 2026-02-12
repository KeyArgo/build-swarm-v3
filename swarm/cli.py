#!/usr/bin/env python3
"""
Build Swarm v3 - CLI Entry Point

Unified command-line interface for the Build Swarm v3 control plane.
All commands except 'serve' communicate with the running server via HTTP API.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# ── ANSI Colors ──────────────────────────────────────────────────────────────

class C:
    """ANSI color codes for terminal output."""
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    RED     = '\033[31m'
    GREEN   = '\033[32m'
    YELLOW  = '\033[33m'
    BLUE    = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN    = '\033[36m'
    WHITE   = '\033[37m'
    BRED    = '\033[91m'
    BGREEN  = '\033[92m'
    BYELLOW = '\033[93m'
    BBLUE   = '\033[94m'
    BMAGENTA= '\033[95m'
    BCYAN   = '\033[96m'

    @staticmethod
    def disable():
        for attr in dir(C):
            if attr.isupper() and not attr.startswith('_'):
                setattr(C, attr, '')


# Disable colors if not a terminal
if not sys.stdout.isatty():
    C.disable()


# ── API Client ───────────────────────────────────────────────────────────────

DEFAULT_URL = 'http://localhost:8100'
BASE_URL = None  # Resolved lazily on first API call

def _resolve_url():
    """Resolve control plane URL once, then cache it."""
    global BASE_URL
    if BASE_URL is not None:
        return BASE_URL
    from swarm.config import discover_control_plane
    BASE_URL = discover_control_plane()
    return BASE_URL


def api_get(path: str, params: dict = None) -> dict:
    """Send a GET request to the control plane API."""
    url = f'{_resolve_url()}{path}'
    if params:
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        url = f'{url}?{query}'
    try:
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        _connection_error(e)
    except Exception as e:
        _connection_error(e)


def api_post(path: str, data: dict = None) -> dict:
    """Send a POST request to the control plane API."""
    url = f'{_resolve_url()}{path}'
    body = json.dumps(data or {}).encode()
    try:
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        _connection_error(e)
    except Exception as e:
        _connection_error(e)


def _resolve_admin_url():
    """Resolve admin dashboard URL (port 8093)."""
    cp_url = _resolve_url()
    # Replace port with admin port
    return cp_url.rsplit(':', 1)[0] + ':8093'


def _get_admin_key():
    """Load admin key from env or key files."""
    key = os.environ.get('SWARM_ADMIN_KEY') or os.environ.get('ADMIN_SECRET')
    if key:
        return key
    for path in ['/etc/build-swarm/admin.key',
                 os.path.expanduser('~/.local/share/build-swarm-v3/admin.key')]:
        try:
            with open(path) as f:
                key = f.read().strip()
                if key:
                    return key
        except (FileNotFoundError, PermissionError):
            continue
    print(f'{C.RED}Error:{C.RESET} No admin key found. Set SWARM_ADMIN_KEY or create /etc/build-swarm/admin.key')
    sys.exit(1)


def admin_get(path: str, params: dict = None) -> dict:
    """GET request to the admin API (port 8093 with auth)."""
    url = f'{_resolve_admin_url()}{path}'
    if params:
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        url = f'{url}?{query}'
    try:
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        req.add_header('X-Admin-Key', _get_admin_key())
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        _connection_error(e)


def admin_post(path: str, data: dict = None) -> dict:
    """POST request to the admin API (port 8093 with auth)."""
    url = f'{_resolve_admin_url()}{path}'
    body = json.dumps(data or {}).encode()
    try:
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        req.add_header('X-Admin-Key', _get_admin_key())
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            return err
        except Exception:
            _connection_error(e)
    except urllib.error.URLError as e:
        _connection_error(e)


def admin_delete(path: str) -> dict:
    """DELETE request to the admin API (port 8093 with auth)."""
    url = f'{_resolve_admin_url()}{path}'
    try:
        req = urllib.request.Request(url, method='DELETE')
        req.add_header('Accept', 'application/json')
        req.add_header('X-Admin-Key', _get_admin_key())
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            return err
        except Exception:
            _connection_error(e)
    except urllib.error.URLError as e:
        _connection_error(e)


def _connection_error(error):
    """Print a clear error message when the server is unreachable."""
    resolved = _resolve_url()
    print(f'\n{C.RED}{C.BOLD}Error:{C.RESET} Cannot connect to control plane at {C.CYAN}{resolved}{C.RESET}')
    print(f'{C.DIM}  Detail: {error}{C.RESET}')
    print()
    print(f'  Is the server running?  {C.YELLOW}build-swarmv3 serve{C.RESET}')
    if os.environ.get('SWARMV3_URL'):
        print(f'  Using custom URL from SWARMV3_URL env var.')
    else:
        print(f'  Or set {C.YELLOW}SWARMV3_URL{C.RESET} if the server is on another host.')
    print()
    sys.exit(1)


# ── Formatting Helpers ───────────────────────────────────────────────────────

def fmt_timestamp(ts):
    """Format a Unix timestamp to human-readable string."""
    if ts is None:
        return '-'
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OSError):
        return str(ts)


def fmt_duration(seconds):
    """Format seconds into a human-readable duration."""
    if seconds is None or seconds == 0:
        return '-'
    seconds = float(seconds)
    if seconds < 60:
        return f'{seconds:.1f}s'
    elif seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f'{m}m {s}s'
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f'{h}h {m}m'


def print_header(title: str):
    """Print a styled section header."""
    print(f'\n{C.BOLD}{C.BCYAN}=== {title} ==={C.RESET}\n')


def print_kv(key: str, value, color=None):
    """Print a key-value pair."""
    vc = color or ''
    vr = C.RESET if color else ''
    print(f'  {C.DIM}{key}:{C.RESET} {vc}{value}{vr}')


def status_color(status: str) -> str:
    """Return ANSI color for a given status string."""
    colors = {
        'online': C.BGREEN,
        'offline': C.RED,
        'active': C.BGREEN,
        'completed': C.GREEN,
        'aborted': C.RED,
        'needed': C.YELLOW,
        'delegated': C.BCYAN,
        'received': C.GREEN,
        'blocked': C.RED,
        'failed': C.BRED,
        'success': C.GREEN,
        'paused': C.BYELLOW,
    }
    return colors.get(status, '')


# ── Command Implementations ──────────────────────────────────────────────────

def cmd_serve(args):
    """Start the control plane HTTP server."""
    from swarm.control_plane import start
    from swarm import __version__

    port = args.port if hasattr(args, 'port') and args.port else None
    db_path = args.db if hasattr(args, 'db') and args.db else None

    print(f'{C.BOLD}{C.BCYAN}Build Swarm v3 Control Plane{C.RESET} {C.DIM}v{__version__}{C.RESET}')
    print(f'{C.DIM}Starting server on port {port or 8100}...{C.RESET}')
    print()

    start(db_path=db_path, port=port)


def cmd_status(args):
    """Show queue status from the running server."""
    data = api_get('/api/v1/status')

    paused = data.get('paused', False)
    version = data.get('version', 'unknown')

    print_header(f'Build Swarm v3 Status')
    print(f'  {C.DIM}Version:{C.RESET} {version}')
    if paused:
        print(f'  {C.BOLD}{C.BYELLOW}*** PAUSED ***{C.RESET}')
    print()

    # Session info
    session = data.get('session')
    if session:
        print(f'  {C.BOLD}Session:{C.RESET} {session.get("id", "?")}')
        print(f'  {C.DIM}Started:{C.RESET} {fmt_timestamp(session.get("started_at"))}')
        print()

    # Queue counts
    print(f'  {C.BOLD}Queue:{C.RESET}')
    needed    = data.get('needed', 0)
    delegated = data.get('delegated', 0)
    received  = data.get('received', 0)
    blocked   = data.get('blocked', 0)
    failed    = data.get('failed', 0)
    total     = data.get('total', 0)

    print(f'    {C.YELLOW}Needed:{C.RESET}    {needed:>5}')
    print(f'    {C.BCYAN}Delegated:{C.RESET} {delegated:>5}')
    print(f'    {C.GREEN}Received:{C.RESET}  {received:>5}')
    print(f'    {C.RED}Blocked:{C.RESET}   {blocked:>5}')
    print(f'    {C.BRED}Failed:{C.RESET}    {failed:>5}')
    print(f'    {C.BOLD}Total:{C.RESET}     {total:>5}')

    if total > 0:
        pct = received / total * 100
        print(f'\n    {C.BGREEN}Progress:{C.RESET} {pct:.1f}%')

    # Drones summary
    drones = data.get('drones', {})
    if drones:
        online = sum(1 for d in drones.values() if d.get('status') == 'online')
        total_drones = len(drones)
        print(f'\n  {C.BOLD}Fleet:{C.RESET} {C.BGREEN}{online}{C.RESET}/{total_drones} drones online')

        # Show active builds
        active = [(did, d) for did, d in drones.items() if d.get('current_task')]
        if active:
            print(f'\n  {C.BOLD}Active Builds:{C.RESET}')
            for did, d in active:
                name = d.get('name', did[:12])
                task = d.get('current_task', '-')
                print(f'    {C.BCYAN}{name:20s}{C.RESET} {task}')

    # Timing stats
    timing = data.get('timing', {})
    if timing and timing.get('total_builds', 0) > 0:
        print(f'\n  {C.BOLD}Stats:{C.RESET}')
        print(f'    Builds:       {timing["total_builds"]}  '
              f'({C.GREEN}{timing["successful"]} ok{C.RESET}, '
              f'{C.RED}{timing["failed"]} failed{C.RESET})')
        print(f'    Success rate: {timing["success_rate"]}%')
        print(f'    Avg duration: {fmt_duration(timing.get("avg_duration_s"))}')
        print(f'    Total time:   {fmt_duration(timing.get("total_duration_s"))}')

    print()


def cmd_fresh(args):
    """Create a fresh session by reading @world and queuing all packages."""
    print_header('Fresh Build Session')
    print(f'  {C.DIM}Running: emerge --pretend --emptytree @world ...{C.RESET}')
    print(f'  {C.DIM}(this may take a moment){C.RESET}')
    print()

    # Run emerge to get the package list
    try:
        result = subprocess.run(
            ['emerge', '--pretend', '--emptytree', '@world'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        print(f'{C.RED}Error:{C.RESET} emerge command not found. '
              f'This must be run on a Gentoo system.')
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f'{C.RED}Error:{C.RESET} emerge command timed out after 5 minutes.')
        sys.exit(1)

    # Parse output for package atoms
    ebuild_re = re.compile(r'^\[ebuild\s+[^\]]*\]\s+(\S+)')
    packages = []

    for line in result.stdout.splitlines():
        m = ebuild_re.match(line.strip())
        if m:
            atom = m.group(1)
            if '::' in atom:
                atom = atom.split('::')[0]
            if not atom.startswith('='):
                atom = f'={atom}'
            packages.append(atom)

    if not packages:
        print(f'{C.YELLOW}Warning:{C.RESET} No packages found in emerge output.')
        if result.returncode != 0:
            print(f'{C.DIM}emerge exited with code {result.returncode}{C.RESET}')
            stderr_out = result.stderr or ''
            if stderr_out:
                for line in stderr_out.strip().splitlines()[:5]:
                    print(f'  {C.DIM}{line}{C.RESET}')
        sys.exit(1)

    print(f'  Found {C.BGREEN}{len(packages)}{C.RESET} packages in @world tree')
    print()

    # Send packages to the control plane
    print(f'  {C.DIM}Sending to control plane...{C.RESET}')
    resp = api_post('/api/v1/queue', {
        'packages': packages,
    })

    queued = resp.get('queued', 0)
    session_id = resp.get('session_id', 'none')

    print(f'  {C.BGREEN}Queued:{C.RESET} {queued} packages')
    if queued < len(packages):
        skipped = len(packages) - queued
        print(f'  {C.DIM}Skipped {skipped} (already in queue){C.RESET}')
    if session_id and session_id != 'none':
        print(f'  {C.DIM}Session: {session_id}{C.RESET}')
    print()


def cmd_queue_add(args):
    """Add packages to the build queue."""
    packages = args.packages
    if not packages:
        print(f'{C.RED}Error:{C.RESET} No packages specified.')
        sys.exit(1)

    resp = api_post('/api/v1/queue', {'packages': packages})
    queued = resp.get('queued', 0)

    print(f'{C.BGREEN}Queued:{C.RESET} {queued}/{len(packages)} packages')
    if queued < len(packages):
        print(f'{C.DIM}Some packages were already in the queue.{C.RESET}')


def cmd_queue_list(args):
    """List current queue contents."""
    data = api_get('/api/v1/status')

    print_header('Build Queue')

    pkgs = data.get('packages', {})

    # Needed packages
    needed = pkgs.get('needed', [])
    if needed:
        total_needed = data.get('needed', len(needed))
        if total_needed > len(needed):
            print(f'  {C.BOLD}{C.YELLOW}Needed ({len(needed)} of {total_needed}):{C.RESET}')
        else:
            print(f'  {C.BOLD}{C.YELLOW}Needed ({len(needed)}):{C.RESET}')
        for pkg in needed:
            print(f'    {pkg}')
        if total_needed > len(needed):
            print(f'    {C.DIM}... and {total_needed - len(needed)} more{C.RESET}')
        print()

    # Delegated packages
    delegated = pkgs.get('delegated', {})
    if delegated:
        print(f'  {C.BOLD}{C.BCYAN}Delegated ({len(delegated)}):{C.RESET}')
        for pkg, info in delegated.items():
            drone = info.get('drone', '?')[:12]
            assigned = fmt_timestamp(info.get('assigned_at'))
            print(f'    {pkg}  {C.DIM}-> {drone} @ {assigned}{C.RESET}')
        print()

    # Blocked packages
    blocked = pkgs.get('blocked', [])
    if blocked:
        print(f'  {C.BOLD}{C.RED}Blocked ({len(blocked)}):{C.RESET}')
        for pkg in blocked:
            print(f'    {pkg}')
        print()

    # Summary
    if not needed and not delegated and not blocked:
        total = data.get('total', 0)
        received = data.get('received', 0)
        if total > 0:
            print(f'  {C.GREEN}Queue clear: {received}/{total} packages received.{C.RESET}')
        else:
            print(f'  {C.DIM}Queue is empty.{C.RESET}')
    print()


def cmd_fleet(args):
    """List registered drones."""
    data = api_get('/api/v1/nodes', {'all': 'true'})
    drones = data.get('drones', [])

    print_header('Drone Fleet')

    if not drones:
        print(f'  {C.DIM}No drones registered.{C.RESET}')
        print()
        return

    # Table header
    print(f'  {C.BOLD}{"Name":<20} {"IP":<16} {"Status":<10} {"Type":<8} '
          f'{"Cores":<6} {"RAM":<7} {"Task"}{C.RESET}')
    print(f'  {C.DIM}{"-"*90}{C.RESET}')

    for d in drones:
        name = d.get('name', '?')
        ip = d.get('ip', '?')
        status = d.get('status', 'unknown')
        ntype = d.get('type', 'drone')
        cores = d.get('cores', '-')
        ram = d.get('ram_gb')
        ram_str = f'{ram:.0f}GB' if ram else '-'
        task = d.get('current_task') or ''
        paused = d.get('paused', False)

        sc = status_color(status)
        status_display = status
        if paused:
            status_display = 'paused'
            sc = C.BYELLOW

        # Truncate long task names
        if len(task) > 40:
            task = task[:37] + '...'

        print(f'  {C.CYAN}{name:<20}{C.RESET} {ip:<16} '
              f'{sc}{status_display:<10}{C.RESET} {ntype:<8} '
              f'{str(cores):<6} {ram_str:<7} {C.DIM}{task}{C.RESET}')

    print(f'\n  {C.DIM}Total: {len(drones)} drones{C.RESET}')

    # Show health issues
    online = [d for d in drones if d.get('status') == 'online']
    offline = [d for d in drones if d.get('status') != 'online']
    print(f'  {C.BGREEN}{len(online)} online{C.RESET}, '
          f'{C.RED}{len(offline)} offline{C.RESET}')
    print()


def cmd_history(args):
    """Show build history."""
    params = {}
    if hasattr(args, 'limit') and args.limit:
        params['limit'] = str(args.limit)

    data = api_get('/api/v1/history', params)
    history = data.get('history', [])
    stats = data.get('stats', {})

    print_header('Build History')

    # Stats summary
    if stats:
        total = stats.get('total_builds', 0)
        success = stats.get('successful', 0)
        failed = stats.get('failed', 0)
        rate = stats.get('success_rate', 0)
        avg = stats.get('avg_duration_s', 0)

        print(f'  {C.BOLD}Summary:{C.RESET} {total} builds  '
              f'({C.GREEN}{success} ok{C.RESET}, {C.RED}{failed} failed{C.RESET})  '
              f'{rate}% success  avg {fmt_duration(avg)}')
        print()

    if not history:
        print(f'  {C.DIM}No build history yet.{C.RESET}')
        print()
        return

    # Table header
    print(f'  {C.BOLD}{"Time":<20} {"Package":<40} {"Drone":<16} '
          f'{"Status":<10} {"Duration"}{C.RESET}')
    print(f'  {C.DIM}{"-"*100}{C.RESET}')

    for entry in history:
        ts = fmt_timestamp(entry.get('built_at'))
        pkg = entry.get('package', '?')
        drone = entry.get('drone_name') or entry.get('drone_id', '?')[:12]
        status = entry.get('status', '?')
        duration = fmt_duration(entry.get('duration_seconds'))
        sc = status_color(status)

        # Truncate long package names
        if len(pkg) > 38:
            pkg = pkg[:35] + '...'
        if len(drone) > 14:
            drone = drone[:11] + '...'

        print(f'  {C.DIM}{ts:<20}{C.RESET} {pkg:<40} {drone:<16} '
              f'{sc}{status:<10}{C.RESET} {duration}')

    count = len(history)
    print(f'\n  {C.DIM}Showing {count} entries{C.RESET}')
    print()


def cmd_control(args):
    """Send a control action to the server."""
    action = args.action

    valid_actions = ['pause', 'resume', 'unblock', 'unground', 'reset',
                     'rebalance', 'clear_failures', 'retry_failures']

    if action not in valid_actions:
        print(f'{C.RED}Error:{C.RESET} Unknown action "{action}"')
        print(f'{C.DIM}Valid actions: {", ".join(valid_actions)}{C.RESET}')
        sys.exit(1)

    payload = {'action': action}

    # For unground, optionally include a drone ID
    if action == 'unground' and hasattr(args, 'target') and args.target:
        payload['drone_id'] = args.target

    resp = api_post('/api/v1/control', payload)

    # Display result
    status = resp.get('status', 'ok')
    sc = status_color(status) or C.BGREEN

    print(f'{C.BOLD}Control:{C.RESET} {action} -> {sc}{status}{C.RESET}')

    # Show extra details from response
    for key in ('unblocked', 'reclaimed', 'affected', 'requeued'):
        if key in resp:
            print(f'  {key}: {resp[key]}')


def cmd_provision(args):
    """Provision a new drone via SSH."""
    ip = args.ip
    name = args.name

    print_header('Drone Provisioning')
    print(f'  {C.DIM}Target:{C.RESET} {C.CYAN}{ip}{C.RESET}')
    if name:
        print(f'  {C.DIM}Name:{C.RESET}   {name}')
    print()

    print(f'  {C.DIM}Initiating SSH provisioning...{C.RESET}')
    resp = api_post('/api/v1/provision/drone', {'ip': ip, 'name': name})

    status = resp.get('status', 'unknown')
    if status == 'provisioning':
        print(f'  {C.BGREEN}Provisioning started{C.RESET}')
        for step in resp.get('steps', []):
            print(f'    {C.DIM}{step}{C.RESET}')
        print()
        print(f'  {C.DIM}The drone should register within 30-60 seconds.{C.RESET}')
        print(f'  {C.DIM}Check: build-swarmv3 fleet{C.RESET}')
    elif status in ('ssh_failed', 'ssh_timeout', 'ssh_not_found'):
        print(f'  {C.RED}Failed:{C.RESET} {resp.get("error", status)}')
        sys.exit(1)
    else:
        print(f'  {C.YELLOW}Status:{C.RESET} {status}')
        if resp.get('error'):
            print(f'  {C.RED}Error:{C.RESET} {resp["error"]}')
    print()


def cmd_switch(args):
    """Switch drones between v2 and v3 control planes."""
    target_version = args.version
    drone_names = args.drones if hasattr(args, 'drones') and args.drones else None
    dry_run = args.dry_run if hasattr(args, 'dry_run') else False

    if target_version not in ('v2', 'v3'):
        print(f'{C.RED}Error:{C.RESET} Version must be "v2" or "v3"')
        sys.exit(1)

    # V2 uses port 8090, v3 uses port 8100
    v2_port = os.environ.get('V2_GATEWAY_PORT', '8090')
    v3_port = os.environ.get('V3_GATEWAY_PORT', '8100')
    gateway_host = os.environ.get('GATEWAY_HOST', '10.0.0.199')

    target_port = v2_port if target_version == 'v2' else v3_port
    target_url = f'http://{gateway_host}:{target_port}'

    print_header(f'Switch Fleet to {target_version.upper()}')
    print(f'  {C.DIM}Target gateway:{C.RESET} {C.CYAN}{target_url}{C.RESET}')
    print()

    # Discover drones from both v2 and v3 APIs
    known_drones = {}  # name -> ip

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
                        known_drones[name] = ip
            print(f'  {C.DIM}Discovered {len(data.get("drones", []))} drones from {label}{C.RESET}')
        except Exception:
            print(f'  {C.DIM}{label} control plane not reachable (port {port}){C.RESET}')

    if not known_drones:
        print(f'\n{C.RED}Error:{C.RESET} No drones discovered from either v2 or v3.')
        print(f'{C.DIM}Make sure at least one control plane is running.{C.RESET}')
        sys.exit(1)

    # Filter to specific drones if requested
    if drone_names:
        filtered = {}
        for name in drone_names:
            if name in known_drones:
                filtered[name] = known_drones[name]
            else:
                print(f'  {C.YELLOW}Warning:{C.RESET} Unknown drone "{name}" '
                      f'(known: {", ".join(sorted(known_drones.keys()))})')
        known_drones = filtered

    if not known_drones:
        print(f'\n{C.RED}Error:{C.RESET} No matching drones found.')
        sys.exit(1)

    print(f'\n  Switching {C.BOLD}{len(known_drones)}{C.RESET} drone(s) '
          f'to {C.BOLD}{target_version.upper()}{C.RESET}:')
    for name, ip in sorted(known_drones.items()):
        print(f'    {C.CYAN}{name:<20}{C.RESET} {ip}')
    print()

    if dry_run:
        print(f'{C.YELLOW}Dry run{C.RESET} — no changes made.')
        return

    # Switch each drone
    results = {'ok': [], 'failed': []}

    for name, ip in sorted(known_drones.items()):
        print(f'  {C.BCYAN}{name}{C.RESET} ({ip})... ', end='', flush=True)

        try:
            # Build the sed commands:
            # 1. Change GATEWAY_URL port
            # 2. For v3: comment out ORCHESTRATOR_IP
            #    For v2: uncomment ORCHESTRATOR_IP
            if target_version == 'v3':
                sed_cmds = (
                    f"sed -i "
                    f"'s|GATEWAY_URL=.*|GATEWAY_URL=\"{target_url}\"|; "
                    f"s|^ORCHESTRATOR_IP=|# ORCHESTRATOR_IP=|' "
                    f"/etc/build-swarm/drone.conf"
                )
            else:
                sed_cmds = (
                    f"sed -i "
                    f"'s|GATEWAY_URL=.*|GATEWAY_URL=\"{target_url}\"|; "
                    f"s|^#\\s*ORCHESTRATOR_IP=|ORCHESTRATOR_IP=|' "
                    f"/etc/build-swarm/drone.conf"
                )

            # Run sed + restart via SSH
            ssh_cmd = [
                'ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
                f'root@{ip}',
                f'{sed_cmds} && rc-service swarm-drone restart 2>/dev/null; '
                f'rc-service swarm-drone start 2>/dev/null; '
                f'grep GATEWAY_URL /etc/build-swarm/drone.conf | head -1'
            ]

            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=30
            )

            if result.returncode == 0 or 'GATEWAY_URL' in result.stdout:
                gateway_line = ''
                for line in result.stdout.splitlines():
                    if 'GATEWAY_URL' in line:
                        gateway_line = line.strip()
                        break
                print(f'{C.BGREEN}OK{C.RESET}  {C.DIM}{gateway_line}{C.RESET}')
                results['ok'].append(name)
            else:
                err = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'unknown error'
                print(f'{C.RED}FAILED{C.RESET}  {C.DIM}{err}{C.RESET}')
                results['failed'].append(name)

        except subprocess.TimeoutExpired:
            print(f'{C.RED}TIMEOUT{C.RESET}')
            results['failed'].append(name)
        except Exception as e:
            print(f'{C.RED}ERROR{C.RESET}  {C.DIM}{e}{C.RESET}')
            results['failed'].append(name)

    # Summary
    print()
    ok = len(results['ok'])
    fail = len(results['failed'])
    total = ok + fail

    if fail == 0:
        print(f'  {C.BGREEN}All {total} drone(s) switched to {target_version.upper()}{C.RESET}')
    else:
        print(f'  {C.GREEN}{ok} switched{C.RESET}, {C.RED}{fail} failed{C.RESET}')
        if results['failed']:
            print(f'  {C.RED}Failed:{C.RESET} {", ".join(results["failed"])}')

    print(f'\n  {C.DIM}Drones will re-register within ~30 seconds.{C.RESET}')
    print(f'  {C.DIM}Check: build-swarmv3 fleet{C.RESET}')
    print()


def cmd_drone(args):
    """Drone image management: audit, deploy, and create."""
    drone_cmd = args.drone_command if hasattr(args, 'drone_command') else None

    if drone_cmd == 'audit':
        _drone_audit(args)
    elif drone_cmd == 'deploy':
        _drone_deploy(args)
    elif drone_cmd == 'create':
        _drone_create(args)
    elif drone_cmd == 'packages':
        _drone_packages(args)
    elif drone_cmd == 'allowlist':
        _drone_allowlist(args)
    elif drone_cmd == 'allow':
        _drone_allow(args)
    elif drone_cmd == 'deny':
        _drone_deny(args)
    elif drone_cmd == 'clean':
        _drone_clean(args)
    elif drone_cmd == 'bloat-audit':
        _drone_bloat_audit(args)
    else:
        print(f'{C.RED}Error:{C.RESET} Specify a drone sub-command: audit, deploy, create, packages, allowlist, allow, deny, clean, bloat-audit')
        sys.exit(1)


def _drone_audit(args):
    """Audit drones against the spec."""
    from swarm.drone_audit import load_spec, audit_drone_ssh, discover_drones
    import concurrent.futures

    targets = args.targets if hasattr(args, 'targets') and args.targets else None
    as_json = args.json if hasattr(args, 'json') else False
    spec_path = args.spec if hasattr(args, 'spec') else None

    try:
        spec = load_spec(spec_path)
    except FileNotFoundError as e:
        print(f'{C.RED}Error:{C.RESET} {e}')
        sys.exit(1)

    # Discover drones
    gateway_host = os.environ.get('GATEWAY_HOST', '10.0.0.199')
    v2_port = os.environ.get('V2_GATEWAY_PORT', '8090')
    v3_port = os.environ.get('V3_GATEWAY_PORT', '8100')

    known_drones = discover_drones(gateway_host, v2_port, v3_port)

    if not known_drones:
        print(f'{C.RED}Error:{C.RESET} No drones discovered. '
              f'Is a control plane running on {gateway_host}?')
        sys.exit(1)

    # Filter to specific targets
    if targets:
        filtered = {}
        for name in targets:
            if name in known_drones:
                filtered[name] = known_drones[name]
            else:
                # Maybe it's an IP address
                for dname, dip in known_drones.items():
                    if dip == name:
                        filtered[dname] = dip
                        break
                else:
                    print(f'{C.YELLOW}Warning:{C.RESET} Unknown drone "{name}" '
                          f'(known: {", ".join(sorted(known_drones.keys()))})')
        known_drones = filtered

    if not known_drones:
        print(f'{C.RED}Error:{C.RESET} No matching drones found.')
        sys.exit(1)

    if not as_json:
        print_header('Drone Compliance Audit')
        print(f'  {C.DIM}Spec:{C.RESET} v{spec.get("spec_version", "?")} '
              f'({spec.get("updated", "?")})')
        print(f'  {C.DIM}Expected profile:{C.RESET} {spec.get("profile", "?")}')
        print(f'  {C.DIM}Targets:{C.RESET} {len(known_drones)} drone(s)')
        print()

    # Audit each drone in parallel
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        for name, ip in sorted(known_drones.items()):
            if not as_json:
                print(f'  {C.DIM}Auditing {name} ({ip})...{C.RESET}')
            futures[executor.submit(audit_drone_ssh, ip, spec)] = name

        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                result['name'] = name
                results[name] = result
            except Exception as e:
                results[name] = {
                    'name': name,
                    'ip': known_drones[name],
                    'status': 'error',
                    'error': str(e),
                    'checks': [],
                }

    # Output
    if as_json:
        # Strip verbose fields from JSON output
        clean = {}
        for name, r in results.items():
            clean[name] = {k: v for k, v in r.items()
                           if k not in ('raw_output', 'raw_stderr')}
        print(json.dumps(clean, indent=2))
        return

    print()
    for name in sorted(results.keys()):
        r = results[name]
        ip = r.get('ip', '?')

        # Status color
        if r['status'] == 'compliant':
            sc = C.BGREEN
        elif r['status'] == 'warnings':
            sc = C.BYELLOW
        elif r['status'] == 'error' or r['status'] == 'timeout':
            sc = C.RED
        else:
            sc = C.BRED

        print(f'  {C.BOLD}{C.CYAN}{name}{C.RESET} ({ip})  '
              f'{sc}{r["status"].upper()}{C.RESET}')

        if r.get('error'):
            print(f'    {C.RED}{r["error"]}{C.RESET}')
            continue

        # Show individual checks
        for check in r.get('checks', []):
            if check['status'] == 'pass':
                icon = f'{C.GREEN}PASS{C.RESET}'
            elif check['status'] == 'warn':
                icon = f'{C.YELLOW}WARN{C.RESET}'
            else:
                icon = f'{C.RED}FAIL{C.RESET}'
            print(f'    {icon}  {check["check"]:<16} {C.DIM}{check["detail"]}{C.RESET}')

        # Summary line
        p = r.get('pass', 0)
        w = r.get('warn', 0)
        f_count = r.get('fail', 0)
        print(f'    {C.DIM}({C.GREEN}{p} pass{C.DIM}, '
              f'{C.YELLOW}{w} warn{C.DIM}, '
              f'{C.RED}{f_count} fail{C.DIM}){C.RESET}')
        print()

    # Overall summary
    total_drones = len(results)
    compliant = sum(1 for r in results.values() if r['status'] == 'compliant')
    warnings = sum(1 for r in results.values() if r['status'] == 'warnings')
    failed = total_drones - compliant - warnings

    print(f'  {C.BOLD}Overall:{C.RESET} '
          f'{C.BGREEN}{compliant}{C.RESET} compliant, '
          f'{C.BYELLOW}{warnings}{C.RESET} warnings, '
          f'{C.BRED}{failed}{C.RESET} non-compliant '
          f'({total_drones} drones)')
    print()


def _drone_deploy(args):
    """Deploy a drone to a target machine."""
    from swarm.drone_audit import deploy_drone_ssh

    ip = args.ip
    name = args.name if hasattr(args, 'name') else None
    prune = args.prune if hasattr(args, 'prune') else False
    dry_run = args.dry_run if hasattr(args, 'dry_run') else False

    # Determine control plane URL
    cp_url = _resolve_url()

    print_header('Drone Deployment')
    print(f'  {C.DIM}Target:{C.RESET}   {C.CYAN}{ip}{C.RESET}')
    if name:
        print(f'  {C.DIM}Name:{C.RESET}     {name}')
    print(f'  {C.DIM}CP URL:{C.RESET}   {cp_url}')
    print(f'  {C.DIM}Prune:{C.RESET}    {prune}')
    print(f'  {C.DIM}Dry Run:{C.RESET}  {dry_run}')
    print()

    print(f'  {C.DIM}Deploying via SSH...{C.RESET}')
    print(f'  {C.DIM}(This may take a while, especially with --prune){C.RESET}')
    print()

    result = deploy_drone_ssh(
        ip=ip,
        cp_url=cp_url,
        name=name,
        prune=prune,
        dry_run=dry_run,
    )

    if result['status'] == 'success':
        print(f'  {C.BGREEN}Deployment successful!{C.RESET}')
        # Print the last ~20 lines of output (the summary)
        if result.get('output'):
            lines = result['output'].strip().splitlines()
            summary_lines = lines[-20:] if len(lines) > 20 else lines
            for line in summary_lines:
                print(f'  {C.DIM}{line}{C.RESET}')
    elif result['status'] in ('ssh_failed', 'ssh_timeout'):
        print(f'  {C.RED}SSH Error:{C.RESET} {result.get("error", "unknown")}')
        sys.exit(1)
    else:
        print(f'  {C.RED}Deployment failed:{C.RESET} {result.get("error", "")}')
        if result.get('errors'):
            for line in result['errors'].strip().splitlines()[-10:]:
                print(f'  {C.DIM}{line}{C.RESET}')
        sys.exit(1)

    print()


def _drone_create(args):
    """Create a new VM/container and bootstrap it as a drone."""
    from swarm.drone_create import create_drone, interactive_create, list_backends

    # Handle --list-backends
    if hasattr(args, 'list_backends') and args.list_backends:
        list_backends()
        return

    # If no backend specified, enter interactive mode
    backend = args.backend if hasattr(args, 'backend') and args.backend else None

    if backend is None:
        try:
            options = interactive_create()
        except (KeyboardInterrupt, EOFError):
            print(f'\n{C.DIM}Aborted.{C.RESET}')
            return

        result = create_drone(**options)
    else:
        # Non-interactive mode
        name = args.name if hasattr(args, 'name') and args.name else None
        host = args.host if hasattr(args, 'host') and args.host else None

        if not name:
            # Auto-generate name
            from swarm.drone_create import _auto_drone_name
            name = _auto_drone_name()
            print(f'{C.DIM}Auto-generated name: {name}{C.RESET}')

        if backend in ('proxmox-lxc', 'proxmox-qemu') and not host:
            print(f'{C.RED}Error:{C.RESET} --host is required for Proxmox backends.')
            print(f'{C.DIM}  Known hosts: 10.0.0.2 (proxmox-io), 10.0.0.3 (proxmox-titan){C.RESET}')
            sys.exit(1)

        # Read SSH key if specified
        ssh_pubkey = None
        ssh_key_path = args.ssh_key if hasattr(args, 'ssh_key') and args.ssh_key else None
        if ssh_key_path:
            try:
                with open(ssh_key_path) as f:
                    ssh_pubkey = f.read().strip()
            except FileNotFoundError:
                print(f'{C.RED}Error:{C.RESET} SSH key not found: {ssh_key_path}')
                sys.exit(1)

        dry_run = args.dry_run if hasattr(args, 'dry_run') else False
        skip_deploy = args.skip_deploy if hasattr(args, 'skip_deploy') else False

        print_header('Drone Creation')
        print(f'  {C.DIM}Backend:{C.RESET}  {C.CYAN}{backend}{C.RESET}')
        print(f'  {C.DIM}Name:{C.RESET}     {name}')
        if host:
            print(f'  {C.DIM}Host:{C.RESET}     {host}')
        print(f'  {C.DIM}Cores:{C.RESET}    {args.cores}')
        print(f'  {C.DIM}RAM:{C.RESET}      {args.ram}MB')
        print(f'  {C.DIM}Disk:{C.RESET}     {args.disk}GB')
        ip_val = args.ip if hasattr(args, 'ip') and args.ip else None
        print(f'  {C.DIM}IP:{C.RESET}       {ip_val or "DHCP"}')
        print(f'  {C.DIM}Dry Run:{C.RESET}  {dry_run}')
        if skip_deploy:
            print(f'  {C.DIM}Deploy:{C.RESET}   skipped (--skip-deploy)')
        print()

        result = create_drone(
            backend=backend,
            name=name,
            host=host,
            ip=ip_val,
            cores=args.cores,
            ram_mb=args.ram,
            disk_gb=args.disk,
            vmid=args.vmid if hasattr(args, 'vmid') else None,
            storage=args.storage if hasattr(args, 'storage') else 'local-lvm',
            bridge=args.bridge if hasattr(args, 'bridge') else 'vmbr0',
            ssh_pubkey=ssh_pubkey,
            dry_run=dry_run,
            skip_deploy=skip_deploy,
        )

    # Display result
    if result['status'] == 'success':
        print(f'\n  {C.BGREEN}{C.BOLD}Drone created successfully!{C.RESET}')
        print(f'  {C.DIM}Name:{C.RESET}     {C.CYAN}{result["name"]}{C.RESET}')
        print(f'  {C.DIM}IP:{C.RESET}       {C.CYAN}{result["ip"]}{C.RESET}')
        print(f'  {C.DIM}Backend:{C.RESET}  {result["backend"]}')
        if result.get('vmid'):
            print(f'  {C.DIM}VMID:{C.RESET}     {result["vmid"]}')
        print()
        print(f'  {C.DIM}The drone should appear in: build-swarmv3 fleet{C.RESET}')
    elif result['status'] == 'partial':
        print(f'\n  {C.YELLOW}VM created but bootstrap failed.{C.RESET}')
        print(f'  {C.DIM}IP:{C.RESET} {result.get("ip", "unknown")}')
        print(f'  {C.DIM}Error:{C.RESET} {result.get("error", "")}')
        print(f'  {C.DIM}Retry with:{C.RESET} build-swarmv3 drone deploy {result.get("ip", "<IP>")}')
    elif result['status'] == 'dry_run':
        pass  # Already printed by orchestrator
    else:
        print(f'\n  {C.RED}Creation failed:{C.RESET} {result.get("error", "unknown error")}')
        if result.get('detail'):
            print(f'  {C.DIM}{result["detail"]}{C.RESET}')
        if result.get('step'):
            print(f'  {C.DIM}Failed at step: {result["step"]}{C.RESET}')
        sys.exit(1)

    print()


def _drone_packages(args):
    """Show installed packages on a drone."""
    name = args.drone_name
    print(f'{C.DIM}Querying {name} via admin API...{C.RESET}')
    data = admin_get(f'/admin/api/drones/{name}/packages')
    if not data or data.get('error'):
        print(f'{C.RED}Error:{C.RESET} {data.get("error", "Unknown error") if data else "No response"}')
        sys.exit(1)

    print_header(f'Packages on {name}')
    print_kv('IP', data.get('ip', '-'))
    print_kv('Profile', data.get('profile', 'unknown'))
    print_kv('Installed', data.get('installed_count', 0))
    print_kv('World', data.get('world_count', 0))

    if data.get('world'):
        print(f'\n  {C.BOLD}@world ({len(data["world"])}):{C.RESET}')
        for pkg in sorted(data['world']):
            print(f'    {pkg}')


def _drone_allowlist(args):
    """Show the drone package allowlist."""
    drone = args.drone_name if hasattr(args, 'drone_name') and args.drone_name else None
    params = {'drone': drone} if drone else {}
    data = admin_get('/admin/api/drones/allowlist', params)
    if not data or data.get('error'):
        print(f'{C.RED}Error:{C.RESET} {data.get("error", "Unknown error") if data else "No response"}')
        sys.exit(1)

    entries = data.get('allowlist', [])
    title = f'Allowlist for {drone}' if drone else 'Global Allowlist'
    print_header(title)

    if not entries:
        print(f'  {C.DIM}No entries.{C.RESET}')
        return

    print(f'  {C.BOLD}{"ID":<6} {"Scope":<15} {"Package":<35} {"Reason":<20}{C.RESET}')
    print(f'  {"─" * 78}')
    for e in entries:
        scope = e.get('drone_id') or 'global'
        reason = e.get('reason') or '-'
        color = C.CYAN if scope == 'global' else C.GREEN
        print(f'  {e["id"]:<6} {color}{scope:<15}{C.RESET} {e["package"]:<35} {C.DIM}{reason:<20}{C.RESET}')


def _drone_allow(args):
    """Add a package to the allowlist."""
    data = {
        'package': args.package,
        'added_by': 'cli',
    }
    if hasattr(args, 'drone') and args.drone:
        data['drone'] = args.drone
    if hasattr(args, 'reason') and args.reason:
        data['reason'] = args.reason

    result = admin_post('/admin/api/drones/allowlist', data)
    if result and result.get('status') == 'ok':
        scope = data.get('drone', 'global')
        print(f'{C.BGREEN}Allowed:{C.RESET} {args.package} (scope: {scope}, id: {result.get("id")})')
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error") if result else "No response"}')
        sys.exit(1)


def _drone_deny(args):
    """Remove a package from the allowlist."""
    # First, find the entry by package name
    data = admin_get('/admin/api/drones/allowlist')
    if not data:
        print(f'{C.RED}Error:{C.RESET} No response from admin API')
        sys.exit(1)

    entries = data.get('allowlist', [])
    matches = [e for e in entries if e['package'] == args.package]

    if not matches:
        print(f'{C.RED}Error:{C.RESET} Package "{args.package}" not found in allowlist')
        sys.exit(1)

    for entry in matches:
        result = admin_delete(f'/admin/api/drones/allowlist/{entry["id"]}')
        scope = entry.get('drone_id') or 'global'
        if result and result.get('status') == 'ok':
            print(f'{C.BRED}Removed:{C.RESET} {args.package} (scope: {scope}, id: {entry["id"]})')
        elif result and result.get('error') and 'protected' in result['error'].lower():
            print(f'{C.RED}PROTECTED:{C.RESET} {args.package} is a critical system package and cannot be removed')
        else:
            err = result.get('error', 'Unknown error') if result else 'No response'
            print(f'{C.RED}Error:{C.RESET} Failed to remove id {entry["id"]}: {err}')


def _drone_clean(args):
    """Clean a drone: two-phase preflight + execute with safety checks.

    Phase 1 (automatic): Runs preflight checks — SSH, building status,
    immutable flags, allowlist sanity, critical package verification.
    Shows full diff of what will change.

    Phase 2 (requires confirmation): Must type the exact drone name
    to confirm. Uses one-time preflight token with 5-minute expiry.
    """
    name = args.drone_name
    dry_run = args.dry_run if hasattr(args, 'dry_run') else False

    # ── Phase 1: Preflight ──────────────────────────────────────────
    print(f'{C.DIM}Running preflight checks on {name}...{C.RESET}')
    result = admin_post(f'/admin/api/drones/{name}/clean/preflight', {})

    if not result:
        print(f'{C.RED}Error:{C.RESET} No response from admin API')
        sys.exit(1)

    if result.get('error'):
        print(f'{C.RED}Error:{C.RESET} {result["error"]}')

    # Display checks
    print_header(f'Pre-Flight Checks: {name}')
    checks = result.get('checks', [])
    for check in checks:
        icon = f'{C.BGREEN}PASS{C.RESET}' if check['passed'] else f'{C.BRED}FAIL{C.RESET}'
        print(f'  [{icon}] {check["name"]}: {check["detail"]}')

    # Display critical packages
    critical = result.get('critical_packages', [])
    if critical:
        print(f'\n  {C.BOLD}Critical System Packages:{C.RESET}')
        for cp_entry in critical:
            pkg = cp_entry['package']
            in_cur = f'{C.GREEN}✓{C.RESET}' if cp_entry['in_current'] else f'{C.RED}✗{C.RESET}'
            in_pro = f'{C.GREEN}✓{C.RESET}' if cp_entry['in_proposed'] else f'{C.RED}✗{C.RESET}'
            print(f'    {pkg:<30s}  current:{in_cur}  proposed:{in_pro}')

    # Display diff
    diff = result.get('diff', {})
    if diff:
        print(f'\n  {C.BOLD}@world Diff:{C.RESET}  '
              f'{diff.get("current_count", "?")} current → {diff.get("proposed_count", "?")} proposed')

        removing = diff.get('removing', [])
        adding = diff.get('adding', [])
        keeping = diff.get('keeping', [])

        if removing:
            print(f'\n  {C.BRED}Removing ({len(removing)}):{C.RESET}')
            for pkg in removing:
                print(f'    {C.RED}- {pkg}{C.RESET}')

        if adding:
            print(f'\n  {C.BGREEN}Adding ({len(adding)}):{C.RESET}')
            for pkg in adding:
                print(f'    {C.GREEN}+ {pkg}{C.RESET}')

        if keeping:
            print(f'\n  {C.DIM}Keeping ({len(keeping)}):{C.RESET}')
            for pkg in keeping:
                print(f'    {C.DIM}  {pkg}{C.RESET}')

    # Abort if preflight failed
    if result.get('status') != 'preflight_ok':
        print(f'\n{C.BRED}Preflight failed. Fix the issues above before cleaning.{C.RESET}')
        sys.exit(1)

    # Dry run stops here
    if dry_run:
        print(f'\n{C.BYELLOW}DRY RUN{C.RESET}: No changes made. '
              f'Remove --dry-run to execute.')
        return

    token = result.get('preflight_token')
    if not token:
        print(f'{C.RED}Error:{C.RESET} No preflight token received')
        sys.exit(1)

    # ── Phase 2: Confirmation + Execute ─────────────────────────────
    print(f'\n{C.BYELLOW}{"═" * 60}{C.RESET}')
    print(f'{C.BYELLOW}  DESTRUCTIVE OPERATION{C.RESET}')
    print(f'{C.BYELLOW}{"═" * 60}{C.RESET}')
    print(f'  This will rewrite @world on {C.BOLD}{name}{C.RESET} and run depclean.')
    if diff.get('removing'):
        print(f'  {C.BRED}{len(diff["removing"])} packages will be removed from @world.{C.RESET}')
    print(f'\n  To confirm, type the exact drone name: {C.BOLD}{name}{C.RESET}')
    print()
    try:
        typed = input(f'  Confirm drone name: ').strip()
    except (KeyboardInterrupt, EOFError):
        print(f'\n{C.DIM}Aborted.{C.RESET}')
        return

    if typed != name:
        print(f'{C.RED}Mismatch:{C.RESET} You typed "{typed}" but the drone is "{name}". Aborted.')
        return

    print(f'\n{C.DIM}Executing clean on {name}...{C.RESET}')
    exec_result = admin_post(f'/admin/api/drones/{name}/clean/execute', {
        'preflight_token': token,
        'confirm_name': name,
    })

    if not exec_result:
        print(f'{C.RED}Error:{C.RESET} No response from admin API')
        sys.exit(1)

    if exec_result.get('error'):
        print(f'{C.RED}Error:{C.RESET} {exec_result["error"]}')
        sys.exit(1)

    print_header(f'Clean Complete: {name}')
    for step in exec_result.get('steps', []):
        print(f'  {C.GREEN}✓{C.RESET} {step}')


def _drone_bloat_audit(args):
    """Audit a drone's installed packages against the allowlist."""
    name = args.drone_name
    print(f'{C.DIM}Auditing {name}...{C.RESET}')
    data = admin_get(f'/admin/api/drones/{name}/audit')
    if not data or data.get('error'):
        print(f'{C.RED}Error:{C.RESET} {data.get("error", "Unknown error") if data else "No response"}')
        sys.exit(1)

    clean = data.get('clean', False)
    status_str = f'{C.BGREEN}CLEAN{C.RESET}' if clean else f'{C.BRED}BLOATED{C.RESET}'

    print_header(f'Bloat Audit: {name}')
    print_kv('Status', status_str)
    print_kv('IP', data.get('ip', '-'))
    print_kv('Profile', data.get('profile', 'unknown'))
    base = data.get('is_base_profile', False)
    print_kv('Base Profile', f'{C.BGREEN}yes{C.RESET}' if base else f'{C.BRED}no{C.RESET}')
    print_kv('Total Installed', data.get('total_installed', 0))
    print_kv('World', data.get('world_count', 0))
    print_kv('Allowed', data.get('allowed_count', 0))
    print_kv('Excess', data.get('excess_count', 0), C.BRED if data.get('excess_count', 0) > 0 else C.BGREEN)
    print_kv('Missing', data.get('missing_count', 0), C.BYELLOW if data.get('missing_count', 0) > 0 else C.BGREEN)

    if data.get('excess'):
        print(f'\n  {C.BRED}Excess packages (in @world, not in allowlist):{C.RESET}')
        for pkg in data['excess']:
            print(f'    {C.RED}✗{C.RESET} {pkg}')

    if data.get('missing'):
        print(f'\n  {C.BYELLOW}Missing packages (in allowlist, not in @world):{C.RESET}')
        for pkg in data['missing']:
            print(f'    {C.YELLOW}?{C.RESET} {pkg}')


def cmd_bootstrap_script(args):
    """Print the drone bootstrap script."""
    resp = api_get('/api/v1/provision/bootstrap')
    if resp is None:
        # The bootstrap endpoint returns text/plain, not JSON
        # Use raw urllib to fetch it
        url = f'{_resolve_url()}/api/v1/provision/bootstrap'
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as r:
                print(r.read().decode())
        except Exception as e:
            print(f'{C.RED}Error:{C.RESET} Could not fetch bootstrap script: {e}')
            sys.exit(1)
    else:
        # If the API proxy returned JSON, try to extract the script
        print(json.dumps(resp, indent=2) if isinstance(resp, dict) else str(resp))


def cmd_monitor(args):
    """Full-screen curses TUI monitor for the build swarm."""
    import curses
    import threading

    interval = args.interval if hasattr(args, 'interval') and args.interval else 5

    # ── Non-fatal API fetcher (doesn't sys.exit on error) ──

    def _api_fetch(path, params=None):
        """Fetch from API without calling sys.exit on failure."""
        url = f'{_resolve_url()}{path}'
        if params:
            query = '&'.join(f'{k}={v}' for k, v in params.items())
            url = f'{url}?{query}'
        try:
            req = urllib.request.Request(url)
            req.add_header('Accept', 'application/json')
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    # ── Color Constants ──

    C_DEFAULT = 1
    C_HEADER = 2
    C_SUCCESS = 3
    C_WARNING = 4
    C_ERROR = 5
    C_INFO = 6
    C_DIM = 7

    def init_colors():
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            try:
                curses.init_pair(C_DEFAULT, -1, -1)
                curses.init_pair(C_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
                curses.init_pair(C_SUCCESS, curses.COLOR_GREEN, -1)
                curses.init_pair(C_WARNING, curses.COLOR_YELLOW, -1)
                curses.init_pair(C_ERROR, curses.COLOR_RED, -1)
                curses.init_pair(C_INFO, curses.COLOR_CYAN, -1)
                curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
            except curses.error:
                pass

    # ── Thread-Safe State ──

    class MonitorState:
        def __init__(self):
            self.lock = threading.Lock()
            self.running = True
            self.paused = False
            self.view_mode = 'dashboard'
            self.status = {}
            self.events = []
            self.last_event_id = 0
            self.binhost = {'packages': 0, 'size_mb': 0}
            self.connected = False
            self.error_msg = None
            self.last_update = None
            self.auto_scroll = True
            self.scroll_offset = 0
            self.session_start = time.time()

    # ── Background Fetcher ──

    class MonitorFetcher(threading.Thread):
        def __init__(self, state):
            super().__init__(daemon=True)
            self.state = state
            self._binhost_tick = 0

        def run(self):
            while self.state.running:
                if not self.state.paused:
                    self._fetch()
                time.sleep(interval)

        def _fetch(self):
            # Main status
            data = _api_fetch('/api/v1/status')
            if data:
                with self.state.lock:
                    self.state.status = data
                    self.state.connected = True
                    self.state.error_msg = None
                    self.state.last_update = time.time()
            else:
                with self.state.lock:
                    self.state.connected = False
                    self.state.error_msg = 'Server unreachable'

            # Events (incremental)
            ev = _api_fetch('/api/v1/events', {'since': str(self.state.last_event_id)})
            if ev and 'events' in ev:
                with self.state.lock:
                    for e in ev['events']:
                        eid = e.get('id', 0)
                        if eid > self.state.last_event_id:
                            self.state.last_event_id = eid
                        self.state.events.append(e)
                    # Keep last 500
                    if len(self.state.events) > 500:
                        self.state.events = self.state.events[-500:]

            # Binhost stats (every ~60s)
            self._binhost_tick += 1
            if self._binhost_tick >= max(1, 60 // interval):
                self._binhost_tick = 0
                bh = _api_fetch('/api/v1/binhost-stats')
                if bh:
                    with self.state.lock:
                        self.state.binhost = bh

        def force_refresh(self):
            threading.Thread(target=self._fetch, daemon=True).start()

    # ── Drawing Helpers ──

    def safe_addstr(win, y, x, text, attr=0, max_x=None):
        """Write string safely, handling curses edge-of-screen errors."""
        h, w = win.getmaxyx()
        if max_x is None:
            max_x = w
        if y < 0 or y >= h or x >= max_x:
            return
        text = str(text)[:max_x - x]
        try:
            win.addstr(y, x, text, attr)
        except curses.error:
            pass

    def draw_box(win, y, x, h, w, title=''):
        """Draw a box with Unicode box-drawing characters."""
        mh, mw = win.getmaxyx()
        if y >= mh or x >= mw:
            return
        w = min(w, mw - x)
        h = min(h, mh - y)
        if h < 2 or w < 4:
            return
        # Top border
        top = '╔═ ' + title + ' ' if title else '╔'
        top += '═' * max(0, w - len(top) - 1) + '╗'
        safe_addstr(win, y, x, top[:w], curses.color_pair(C_INFO))
        # Sides
        for row in range(1, h - 1):
            if y + row < mh:
                safe_addstr(win, y + row, x, '║', curses.color_pair(C_INFO))
                if x + w - 1 < mw:
                    safe_addstr(win, y + row, x + w - 1, '║', curses.color_pair(C_INFO))
        # Bottom border
        bot = '╚' + '═' * max(0, w - 2) + '╝'
        safe_addstr(win, y + h - 1, x, bot[:w], curses.color_pair(C_INFO))

    def draw_bar(win, y, x, width, val, total):
        """Draw a progress bar with block characters."""
        if total == 0:
            total = 1
        pct = min(1.0, val / total)
        filled = int(width * pct)
        bar = '█' * filled + '░' * (width - filled)
        safe_addstr(win, y, x, bar[:filled], curses.color_pair(C_SUCCESS))
        safe_addstr(win, y, x + filled, bar[filled:], curses.color_pair(C_DIM))

    # ── Dashboard View ──

    def draw_dashboard(stdscr, state):
        h, w = stdscr.getmaxyx()
        if h < 10 or w < 40:
            safe_addstr(stdscr, 0, 0, 'Terminal too small', curses.color_pair(C_ERROR))
            return

        with state.lock:
            data = dict(state.status)
            events = list(state.events)
            binhost = dict(state.binhost)

        version = data.get('version', '?')
        nodes_on = data.get('nodes_online', 0)
        nodes_total = data.get('nodes', 0)
        total_cores = data.get('total_cores', 0)
        paused = data.get('paused', False)
        needed = data.get('needed', 0)
        delegated = data.get('delegated', 0)
        received = data.get('received', 0)
        blocked = data.get('blocked', 0)
        failed = data.get('failed', 0)
        total = data.get('total', 0)
        drones = data.get('drones', {})
        timing = data.get('timing', {})
        pkgs = data.get('packages', {})

        # Layout
        half_w = w // 2

        # ── Row 0-2: Control Plane + Binhost ──
        cp_w = half_w
        bh_w = w - half_w

        draw_box(stdscr, 0, 0, 4, cp_w, 'CONTROL PLANE')
        cp_status = '● ' if state.connected else '○ '
        cp_line1 = f'{cp_status}v{version}  {_resolve_url()}'
        paused_str = '  [PAUSED]' if paused else ''
        cp_line2 = f'{nodes_on}/{nodes_total} online, {total_cores} cores{paused_str}'
        attr = curses.color_pair(C_SUCCESS) if state.connected else curses.color_pair(C_ERROR)
        safe_addstr(stdscr, 1, 2, cp_line1, attr, cp_w - 1)
        safe_addstr(stdscr, 2, 2, cp_line2, curses.color_pair(C_DEFAULT), cp_w - 1)

        draw_box(stdscr, 0, half_w, 4, bh_w, 'BINHOST')
        bh_pkgs = binhost.get('packages', 0)
        bh_size = binhost.get('size_mb', 0)
        if bh_size >= 1024:
            bh_size_str = f'{bh_size/1024:.1f}G'
        else:
            bh_size_str = f'{bh_size}M'
        safe_addstr(stdscr, 1, half_w + 2, f'Production: {bh_pkgs} pkgs  ({bh_size_str})',
                    curses.color_pair(C_DEFAULT), w - 1)
        success_rate = timing.get('success_rate', 0)
        total_builds = timing.get('total_builds', 0)
        safe_addstr(stdscr, 2, half_w + 2, f'Success: {success_rate}%  Builds: {total_builds}',
                    curses.color_pair(C_DEFAULT), w - 1)

        # ── Row 4-7: Build Progress ──
        draw_box(stdscr, 4, 0, 5, w, 'BUILD PROGRESS')
        bar_w = min(w - 30, 60)
        if bar_w > 5 and total > 0:
            pct = received / total * 100
            draw_bar(stdscr, 5, 2, bar_w, received, total)
            safe_addstr(stdscr, 5, bar_w + 3, f'{received}/{total} ({pct:.0f}%)',
                        curses.color_pair(C_DEFAULT))
        elif total == 0:
            safe_addstr(stdscr, 5, 2, 'No active session — run: build-swarmv3 fresh',
                        curses.color_pair(C_DIM))

        stats_line = f'Needed: {needed}  |  Building: {delegated}  |  Complete: {received}  |  Blocked: {blocked}'
        if failed:
            stats_line += f'  |  Failed: {failed}'
        safe_addstr(stdscr, 6, 2, stats_line, curses.color_pair(C_DEFAULT), w - 2)

        # Rate + ETA
        elapsed = time.time() - state.session_start
        if received > 0 and elapsed > 10:
            rate = received * 60 / elapsed
            remaining = total - received
            if rate > 0 and remaining > 0:
                eta_s = remaining / (rate / 60)
                eta_str = fmt_duration(eta_s)
            elif remaining <= 0:
                eta_str = 'complete'
            else:
                eta_str = '...'
            safe_addstr(stdscr, 7, 2, f'Rate: {rate:.1f} pkg/min  |  ETA: {eta_str}',
                        curses.color_pair(C_DIM), w - 2)

        # ── Row 9+: Drones Table ──
        drone_list = sorted(drones.items(), key=lambda kv: (
            0 if kv[1].get('current_task') else 1,
            0 if kv[1].get('status') == 'online' else 1,
            kv[0]
        ))
        drone_h = min(len(drone_list) + 3, max(5, h - 21))
        draw_box(stdscr, 9, 0, drone_h, w, 'DRONES (CPU% | RAM% | Load | Cores | Task)')

        # Header row
        hdr = f'{"Name":<16} {"IP":<18} {"CPU":>4} {"RAM":>4} {"Load":>5} {"Cores":>5}  {"Task"}'
        safe_addstr(stdscr, 10, 2, hdr, curses.A_BOLD | curses.color_pair(C_DIM), w - 3)

        for i, (dname, d) in enumerate(drone_list):
            row = 11 + i
            if row >= 9 + drone_h - 1:
                break
            m = d.get('metrics', {})
            caps = d.get('capabilities', {})
            task = d.get('current_task', '')
            status = d.get('status', 'offline')
            cpu = m.get('cpu_percent', 0)
            ram = m.get('ram_percent', 0)
            load = m.get('load_1m', 0)
            cores = caps.get('cores', '?')
            ip = d.get('ip', '?')

            if status == 'online' and task:
                dot = '●'
                dot_color = curses.color_pair(C_SUCCESS)
                # Shorten task: "sys-devel/gcc" → "gcc"
                task_short = task.split('/')[-1] if '/' in task else task
            elif status == 'online':
                dot = '●'
                dot_color = curses.color_pair(C_INFO)
                task_short = '(idle)'
            else:
                dot = '○'
                dot_color = curses.color_pair(C_DIM)
                task_short = '(offline)'

            safe_addstr(stdscr, row, 2, dot, dot_color)
            line = f' {dname:<15} {ip:<18} {cpu:>3.0f}% {ram:>3.0f}% {load:>5.1f} {cores:>5}  {task_short}'
            attr = curses.color_pair(C_DEFAULT) if status == 'online' else curses.color_pair(C_DIM)
            safe_addstr(stdscr, row, 3, line, attr, w - 4)

        # ── Bottom Panels: Active Assignments + Recent Events ──
        bot_y = 9 + drone_h
        remaining_h = h - bot_y - 1  # 1 for status bar
        if remaining_h < 4:
            return
        panel_h = remaining_h
        assign_w = half_w
        events_w = w - half_w

        draw_box(stdscr, bot_y, 0, panel_h, assign_w, 'ACTIVE ASSIGNMENTS')
        del_pkgs = pkgs.get('delegated', {})
        if isinstance(del_pkgs, dict):
            for i, (pkg, info) in enumerate(del_pkgs.items()):
                row = bot_y + 1 + i
                if row >= bot_y + panel_h - 1:
                    break
                drone = info.get('drone', '?') if isinstance(info, dict) else str(info)
                pkg_short = pkg.split('/')[-1] if '/' in pkg else pkg
                drone_short = drone.replace('drone-', '') if drone.startswith('drone-') else drone
                line = f'{pkg_short} → {drone_short}'
                safe_addstr(stdscr, row, 2, line, curses.color_pair(C_INFO), assign_w - 3)
        if not del_pkgs:
            safe_addstr(stdscr, bot_y + 1, 2, '(none)', curses.color_pair(C_DIM))

        draw_box(stdscr, bot_y, half_w, panel_h, events_w, 'RECENT EVENTS')
        visible = panel_h - 2
        recent = events[-visible:] if events else []
        for i, ev in enumerate(recent):
            row = bot_y + 1 + i
            if row >= bot_y + panel_h - 1:
                break
            ts = ev.get('timestamp', 0)
            ts_str = datetime.fromtimestamp(ts).strftime('%H:%M') if ts else '??:??'
            msg = ev.get('message', '')[:events_w - 12]
            etype = ev.get('type', '')

            if etype in ('complete', 'recv'):
                color = curses.color_pair(C_SUCCESS)
            elif etype in ('fail', 'grounded'):
                color = curses.color_pair(C_ERROR)
            elif etype in ('assign', 'rebalance'):
                color = curses.color_pair(C_INFO)
            else:
                color = curses.color_pair(C_DIM)

            safe_addstr(stdscr, row, half_w + 2, f'[{ts_str}] {msg}', color, w - 1)

    # ── Log View ──

    def draw_log_view(stdscr, state):
        h, w = stdscr.getmaxyx()
        with state.lock:
            events = list(state.events)

        count = len(events)
        title = f'EVENT LOG ({count} entries)'
        if not state.auto_scroll:
            title += ' [SCROLL LOCKED]'
        safe_addstr(stdscr, 0, 0, f' {title} '.ljust(w),
                    curses.color_pair(C_HEADER) | curses.A_BOLD)

        visible = h - 2
        if visible < 1:
            return

        if state.auto_scroll:
            start = max(0, count - visible)
        else:
            start = max(0, min(state.scroll_offset, count - 1))
        end = min(start + visible, count)

        for i, idx in enumerate(range(start, end)):
            row = 1 + i
            if row >= h - 1:
                break
            ev = events[idx]
            ts = ev.get('timestamp', 0)
            ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts else '??:??:??'
            etype = ev.get('type', '?')
            msg = ev.get('message', '')
            line = f'[{ts_str}] [{etype:>10}] {msg}'

            if etype in ('complete', 'recv'):
                color = curses.color_pair(C_SUCCESS)
            elif etype in ('fail', 'grounded', 'stale'):
                color = curses.color_pair(C_ERROR)
            elif etype in ('assign', 'rebalance'):
                color = curses.color_pair(C_INFO)
            elif etype in ('register', 'offline'):
                color = curses.color_pair(C_WARNING)
            else:
                color = curses.color_pair(C_DEFAULT)

            safe_addstr(stdscr, row, 0, line[:w - 1], color)

    # ── Status Bar ──

    def draw_statusbar(stdscr, state):
        h, w = stdscr.getmaxyx()
        row = h - 1

        if state.view_mode == 'dashboard':
            keys = '[q]uit [l]ogs [r]efresh [p]ause'
        else:
            keys = '[q]uit [d]ashboard [↑↓]scroll [End]follow'

        paused_str = ' [PAUSED]' if state.paused else ''
        if state.last_update:
            ts = datetime.fromtimestamp(state.last_update).strftime('%H:%M:%S')
        else:
            ts = '--:--:--'

        if state.error_msg:
            right = f'  {state.error_msg}  '
            attr = curses.color_pair(C_ERROR)
        else:
            right = f'  Updated: {ts}{paused_str}  '
            attr = curses.color_pair(C_DIM)

        line = f' {keys}'.ljust(w - len(right)) + right
        safe_addstr(stdscr, row, 0, line[:w], attr)

    # ── Main Curses Loop ──

    def _monitor_main(stdscr):
        init_colors()
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(100)

        state = MonitorState()
        fetcher = MonitorFetcher(state)
        fetcher.start()

        while True:
            try:
                ch = stdscr.getch()
                if ch == ord('q') or ch == ord('Q'):
                    break
                elif ch == ord('p') or ch == ord('P'):
                    state.paused = not state.paused
                elif ch == ord('r') or ch == ord('R'):
                    fetcher.force_refresh()
                elif ch == ord('l') or ch == ord('L'):
                    state.view_mode = 'log'
                elif ch == ord('d') or ch == ord('D'):
                    state.view_mode = 'dashboard'

                # Scroll in log mode
                if state.view_mode == 'log':
                    if ch == curses.KEY_UP:
                        if state.auto_scroll:
                            with state.lock:
                                state.scroll_offset = max(0, len(state.events) - (curses.LINES - 2))
                        state.auto_scroll = False
                        state.scroll_offset = max(0, state.scroll_offset - 1)
                    elif ch == curses.KEY_DOWN:
                        state.scroll_offset += 1
                        with state.lock:
                            if state.scroll_offset >= len(state.events) - (curses.LINES - 2):
                                state.auto_scroll = True
                    elif ch == curses.KEY_PPAGE:
                        state.auto_scroll = False
                        state.scroll_offset = max(0, state.scroll_offset - 10)
                    elif ch == curses.KEY_NPAGE:
                        state.scroll_offset += 10
                    elif ch == curses.KEY_END:
                        state.auto_scroll = True

                stdscr.erase()

                if state.view_mode == 'dashboard':
                    draw_dashboard(stdscr, state)
                else:
                    draw_log_view(stdscr, state)

                draw_statusbar(stdscr, state)
                stdscr.refresh()
                time.sleep(0.05)

            except KeyboardInterrupt:
                break
            except curses.error:
                pass

        state.running = False

    curses.wrapper(_monitor_main)


# ── Release Commands ─────────────────────────────────────────────────────────

def cmd_release(args):
    """Dispatch release subcommands."""
    subcmd = getattr(args, 'release_command', None)
    if not subcmd:
        print(f'{C.RED}Error:{C.RESET} Specify: create, list, show, promote, rollback, archive, delete, diff, migrate')
        sys.exit(1)

    dispatch = {
        'create':  cmd_release_create,
        'list':    cmd_release_list,
        'show':    cmd_release_show,
        'promote': cmd_release_promote,
        'rollback': cmd_release_rollback,
        'archive': cmd_release_archive,
        'delete':  cmd_release_delete,
        'diff':    cmd_release_diff,
        'migrate': cmd_release_migrate,
    }
    fn = dispatch.get(subcmd)
    if fn:
        fn(args)
    else:
        print(f'{C.RED}Error:{C.RESET} Unknown release command: {subcmd}')
        sys.exit(1)


def cmd_release_list(args):
    """List all releases."""
    data = admin_get('/admin/api/releases')
    releases = data.get('releases', [])
    print_header('Releases')
    if not releases:
        print(f'  {C.DIM}No releases found. Run: build-swarmv3 release migrate{C.RESET}')
        return

    # Header row
    print(f'  {C.BOLD}{"Version":<20} {"Status":<10} {"Packages":>8} {"Size":>8}  {"Created":<20} {"Promoted":<20}{C.RESET}')
    print(f'  {"─" * 90}')

    for r in releases:
        sc = status_color(r['status'])
        marker = ' *' if r['status'] == 'active' else ''
        promoted = fmt_timestamp(r.get('promoted_at')) if r.get('promoted_at') else '-'
        print(f'  {sc}{r["version"]:<20}{C.RESET} '
              f'{sc}{r["status"]:<10}{C.RESET} '
              f'{r.get("package_count", 0):>8} '
              f'{r.get("size_mb", 0):>7.0f}M  '
              f'{fmt_timestamp(r.get("created_at")):<20} '
              f'{promoted:<20}'
              f'{marker}')


def cmd_release_create(args):
    """Create a new release from staging."""
    data = {}
    if hasattr(args, 'name') and args.name:
        data['version'] = args.name
    if hasattr(args, 'notes') and args.notes:
        data['notes'] = args.notes
    result = admin_post('/admin/api/releases', data)
    if result.get('status') == 'ok':
        print_header('Release Created')
        print_kv('Version', result['version'], C.BGREEN)
        print_kv('Packages', result.get('package_count', 0))
        print_kv('Size', f"{result.get('size_mb', 0)} MB")
        print_kv('Path', result.get('path', '-'), C.DIM)
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error")}')
        sys.exit(1)


def cmd_release_show(args):
    """Show release details."""
    result = admin_get(f'/admin/api/releases/{args.version}')
    if result.get('error'):
        print(f'{C.RED}Error:{C.RESET} {result["error"]}')
        sys.exit(1)
    print_header(f'Release: {args.version}')
    sc = status_color(result.get('status', ''))
    print_kv('Version', result['version'], C.BGREEN)
    if result.get('name'):
        print_kv('Name', result['name'])
    print_kv('Status', f'{sc}{result["status"]}{C.RESET}')
    print_kv('Packages', result.get('package_count', 0))
    print_kv('Size', f"{result.get('size_mb', 0)} MB")
    print_kv('Path', result.get('path', '-'), C.DIM)
    print_kv('Created', fmt_timestamp(result.get('created_at')))
    if result.get('promoted_at'):
        print_kv('Promoted', fmt_timestamp(result['promoted_at']))
    if result.get('notes'):
        print_kv('Notes', result['notes'])


def cmd_release_promote(args):
    """Promote a release to active."""
    result = admin_post(f'/admin/api/releases/{args.version}/promote')
    if result.get('status') == 'ok':
        print(f'{C.BGREEN}Promoted{C.RESET} {args.version} to active')
        if result.get('previous'):
            print(f'  Previous: {result["previous"]} → archived')
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error")}')
        sys.exit(1)


def cmd_release_rollback(args):
    """Rollback to previous release."""
    result = admin_post('/admin/api/releases/rollback')
    if result.get('status') == 'ok':
        print(f'{C.BGREEN}Rolled back{C.RESET} to {result.get("version", "previous")}')
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error")}')
        sys.exit(1)


def cmd_release_archive(args):
    """Archive a release."""
    result = admin_post(f'/admin/api/releases/{args.version}/archive')
    if result.get('status') == 'ok':
        print(f'{C.YELLOW}Archived{C.RESET} {args.version}')
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error")}')
        sys.exit(1)


def cmd_release_delete(args):
    """Delete an archived release."""
    result = admin_delete(f'/admin/api/releases/{args.version}')
    if result.get('status') == 'ok':
        print(f'{C.RED}Deleted{C.RESET} {args.version}')
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error")}')
        sys.exit(1)


def cmd_release_diff(args):
    """Compare two releases."""
    data = admin_get('/admin/api/releases/diff',
                     {'from': args.from_version, 'to': args.to_version})
    if data.get('error'):
        print(f'{C.RED}Error:{C.RESET} {data["error"]}')
        sys.exit(1)

    summary = data.get('summary', {})
    print_header(f'Diff: {args.from_version} → {args.to_version}')
    print_kv('Added', summary.get('added', 0), C.BGREEN)
    print_kv('Removed', summary.get('removed', 0), C.BRED)
    print_kv('Changed', summary.get('changed', 0), C.BYELLOW)
    print_kv('Unchanged', summary.get('unchanged', 0), C.DIM)

    if data.get('added'):
        print(f'\n  {C.BGREEN}+ Added:{C.RESET}')
        for p in data['added']:
            print(f'    {p["category"]}/{p["package"]}-{p["version"]}')

    if data.get('removed'):
        print(f'\n  {C.BRED}- Removed:{C.RESET}')
        for p in data['removed']:
            print(f'    {p["category"]}/{p["package"]}-{p["version"]}')

    if data.get('changed'):
        print(f'\n  {C.BYELLOW}~ Changed:{C.RESET}')
        for p in data['changed']:
            print(f'    {p["category"]}/{p["package"]}: {p["from_version"]} → {p["to_version"]}')


def cmd_release_migrate(args):
    """One-time migration to release-based layout."""
    result = admin_post('/admin/api/releases/migrate')
    if result.get('status') == 'ok':
        print_header('Migration Complete')
        print_kv('Version', result['version'], C.BGREEN)
        print_kv('Packages', result.get('package_count', 0))
        print_kv('Size', f"{result.get('size_mb', 0)} MB")
        print_kv('Symlink', result.get('symlink', '-'), C.DIM)
    else:
        print(f'{C.RED}Error:{C.RESET} {result.get("error", "Unknown error")}')
        sys.exit(1)


# ── Argument Parser ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog='build-swarmv3',
        description='Build Swarm v3 - Distributed Gentoo Binary Package Builder',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f'''{C.DIM}Examples:
  build-swarmv3 serve                   Start the control plane server
  build-swarmv3 status                  Show current queue status
  build-swarmv3 fresh                   Queue all @world packages
  build-swarmv3 queue add cat/pkg-1.0   Add packages to queue
  build-swarmv3 control pause           Pause the build queue
  build-swarmv3 monitor                 Live status display
  build-swarmv3 drone audit             Audit all drones against spec
  build-swarmv3 drone deploy 10.0.0.x   Deploy a drone to a target machine
  build-swarmv3 switch v3               Switch all drones to v3

Environment:
  SWARMV3_URL          Server URL (default: http://localhost:8100)
  CONTROL_PLANE_PORT   Port for serve command (default: 8100)
  SWARM_DB_PATH        Database path for serve command{C.RESET}'''
    )

    parser.add_argument('--no-color', action='store_true',
                        help='Disable colored output')

    sub = parser.add_subparsers(dest='command', help='Command to run')

    # serve
    p_serve = sub.add_parser('serve', help='Start the control plane HTTP server')
    p_serve.add_argument('--port', type=int, default=None,
                         help='Port to listen on (default: 8100)')
    p_serve.add_argument('--db', type=str, default=None,
                         help='Path to SQLite database')

    # status
    sub.add_parser('status', help='Show queue status')

    # fresh
    sub.add_parser('fresh', help='Create a fresh session from @world')

    # queue (subcommands)
    p_queue = sub.add_parser('queue', help='Manage the build queue')
    queue_sub = p_queue.add_subparsers(dest='queue_command', help='Queue sub-command')

    p_qadd = queue_sub.add_parser('add', help='Add packages to queue')
    p_qadd.add_argument('packages', nargs='+', help='Package atoms to add')

    queue_sub.add_parser('list', help='List queue contents')

    # fleet
    sub.add_parser('fleet', help='List registered drones')

    # history
    p_hist = sub.add_parser('history', help='Show build history')
    p_hist.add_argument('--limit', type=int, default=50,
                        help='Number of entries to show (default: 50)')

    # control
    p_ctrl = sub.add_parser('control', help='Send control action')
    p_ctrl.add_argument('action',
                        help='Action: pause, resume, unblock, unground, reset, '
                             'rebalance, clear_failures, retry_failures')
    p_ctrl.add_argument('target', nargs='?', default=None,
                        help='Optional target (e.g., drone ID for unground)')

    # monitor
    p_mon = sub.add_parser('monitor', help='Live status display')
    p_mon.add_argument('--interval', type=int, default=5,
                       help='Refresh interval in seconds (default: 5)')

    # provision
    p_prov = sub.add_parser('provision', help='Provision a new drone via SSH')
    p_prov.add_argument('ip', help='IP address of the target machine')
    p_prov.add_argument('--name', type=str, default=None,
                        help='Name for the drone (default: hostname)')

    # bootstrap-script
    sub.add_parser('bootstrap-script',
                   help='Print the drone bootstrap shell script')

    # drone (subcommands)
    p_drone = sub.add_parser('drone', help='Drone image management')
    drone_sub = p_drone.add_subparsers(dest='drone_command',
                                        help='Drone sub-command')

    p_audit = drone_sub.add_parser('audit', help='Audit drones against spec')
    p_audit.add_argument('targets', nargs='*',
                         help='Drone names or IPs (default: all)')
    p_audit.add_argument('--json', action='store_true',
                         help='Output results as JSON')
    p_audit.add_argument('--spec', type=str, default=None,
                         help='Path to drone.spec file')

    p_deploy = drone_sub.add_parser('deploy',
                                     help='Deploy drone to a target machine')
    p_deploy.add_argument('ip', help='Target IP address')
    p_deploy.add_argument('--name', type=str, default=None,
                          help='Drone name (default: target hostname)')
    p_deploy.add_argument('--prune', action='store_true',
                          help='Remove extra packages (emerge --depclean)')
    p_deploy.add_argument('--dry-run', action='store_true',
                          help='Show what would change without doing it')

    p_create = drone_sub.add_parser('create',
                                     help='Create a new VM/container and bootstrap as drone',
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=f'''{C.DIM}Examples:
  build-swarmv3 drone create                                  Interactive wizard
  build-swarmv3 drone create -b docker -n drone-05            Docker container
  build-swarmv3 drone create -b proxmox-lxc -H 10.0.0.2 -n drone-05
  build-swarmv3 drone create -b qemu -n drone-qemu-01
  build-swarmv3 drone create --list-backends                  Show backends
  build-swarmv3 drone create -b docker --dry-run              Preview{C.RESET}''')
    p_create.add_argument('--backend', '-b', type=str, default=None,
                          help='Backend: docker, proxmox-lxc, proxmox-qemu, qemu')
    p_create.add_argument('--host', '-H', type=str, default=None,
                          help='Hypervisor host IP (required for Proxmox)')
    p_create.add_argument('--name', '-n', type=str, default=None,
                          help='Drone name (e.g., drone-05)')
    p_create.add_argument('--ip', type=str, default=None,
                          help='Static IP (default: DHCP)')
    p_create.add_argument('--cores', type=int, default=4,
                          help='CPU cores (default: 4)')
    p_create.add_argument('--ram', type=int, default=4096,
                          help='RAM in MB (default: 4096)')
    p_create.add_argument('--disk', type=int, default=50,
                          help='Disk in GB (default: 50)')
    p_create.add_argument('--vmid', type=int, default=None,
                          help='VM/container ID (Proxmox, default: auto)')
    p_create.add_argument('--storage', type=str, default='local-lvm',
                          help='Storage pool (default: local-lvm)')
    p_create.add_argument('--bridge', type=str, default='vmbr0',
                          help='Network bridge (default: vmbr0)')
    p_create.add_argument('--ssh-key', type=str, default=None,
                          help='SSH public key file (default: auto-detect)')
    p_create.add_argument('--skip-deploy', action='store_true',
                          help='Create VM only, skip drone bootstrap')
    p_create.add_argument('--dry-run', action='store_true',
                          help='Show what would happen without doing it')
    p_create.add_argument('--list-backends', action='store_true',
                          help='List available backends and exit')

    # drone packages
    p_dpkg = drone_sub.add_parser('packages', help='List installed packages on a drone')
    p_dpkg.add_argument('drone_name', help='Drone name (e.g., Imai)')

    # drone allowlist
    p_dal = drone_sub.add_parser('allowlist', help='Show the package allowlist')
    p_dal.add_argument('drone_name', nargs='?', default=None,
                       help='Drone name to filter (default: show all)')

    # drone allow
    p_dallow = drone_sub.add_parser('allow', help='Add a package to the allowlist')
    p_dallow.add_argument('package', help='Package atom (e.g., net-misc/openssh)')
    p_dallow.add_argument('--drone', type=str, default=None,
                          help='Drone name for per-drone allowance (default: global)')
    p_dallow.add_argument('--reason', type=str, default=None,
                          help='Reason for allowing this package')

    # drone deny
    p_ddeny = drone_sub.add_parser('deny', help='Remove a package from the allowlist')
    p_ddeny.add_argument('package', help='Package atom to remove')

    # drone clean
    p_dclean = drone_sub.add_parser('clean',
                                     help='Clean a drone: switch to base profile + depclean')
    p_dclean.add_argument('drone_name', help='Drone name to clean')
    p_dclean.add_argument('--dry-run', action='store_true',
                          help='Show what would be done without doing it')

    # drone bloat-audit
    p_dbaudit = drone_sub.add_parser('bloat-audit',
                                      help='Audit drone packages against allowlist')
    p_dbaudit.add_argument('drone_name', help='Drone name to audit')

    # release
    p_release = sub.add_parser('release', help='Manage binary package releases')
    release_sub = p_release.add_subparsers(dest='release_command', help='Release sub-command')

    release_sub.add_parser('list', help='List all releases')

    p_rc = release_sub.add_parser('create', help='Create a new release from staging')
    p_rc.add_argument('--name', type=str, default=None,
                      help='Version name (default: auto YYYY.MM.DD)')
    p_rc.add_argument('--notes', type=str, default=None, help='Release notes')

    p_rs = release_sub.add_parser('show', help='Show release details')
    p_rs.add_argument('version', help='Release version to show')

    p_rp = release_sub.add_parser('promote', help='Promote a release to active')
    p_rp.add_argument('version', help='Release version to promote')

    release_sub.add_parser('rollback', help='Switch to previous release')

    p_ra = release_sub.add_parser('archive', help='Archive a release')
    p_ra.add_argument('version', help='Release version to archive')

    p_rd = release_sub.add_parser('delete', help='Delete an archived release')
    p_rd.add_argument('version', help='Release version to delete')

    p_rdiff = release_sub.add_parser('diff', help='Compare two releases')
    p_rdiff.add_argument('from_version', help='Source release version')
    p_rdiff.add_argument('to_version', help='Target release version')

    release_sub.add_parser('migrate', help='One-time migration to release-based layout')

    # switch
    p_switch = sub.add_parser('switch',
                              help='Switch drones between v2 and v3 control planes')
    p_switch.add_argument('version', choices=['v2', 'v3'],
                          help='Target version (v2 or v3)')
    p_switch.add_argument('drones', nargs='*', default=None,
                          help='Specific drone names (default: all)')
    p_switch.add_argument('--dry-run', action='store_true',
                          help='Show what would be changed without doing it')

    return parser


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.no_color:
        C.disable()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        'serve':            cmd_serve,
        'status':           cmd_status,
        'fresh':            cmd_fresh,
        'fleet':            cmd_fleet,
        'history':          cmd_history,
        'control':          cmd_control,
        'monitor':          cmd_monitor,
        'provision':        cmd_provision,
        'bootstrap-script': cmd_bootstrap_script,
        'switch':           cmd_switch,
    }

    if args.command == 'queue':
        if args.queue_command == 'add':
            cmd_queue_add(args)
        elif args.queue_command == 'list':
            cmd_queue_list(args)
        else:
            print(f'{C.RED}Error:{C.RESET} Specify a queue sub-command: add, list')
            sys.exit(1)
    elif args.command == 'release':
        cmd_release(args)
    elif args.command == 'drone':
        cmd_drone(args)
    elif args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
