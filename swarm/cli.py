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
BASE_URL = os.environ.get('SWARMV3_URL', DEFAULT_URL)


def api_get(path: str, params: dict = None) -> dict:
    """Send a GET request to the control plane API."""
    url = f'{BASE_URL}{path}'
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
    url = f'{BASE_URL}{path}'
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


def _connection_error(error):
    """Print a clear error message when the server is unreachable."""
    print(f'\n{C.RED}{C.BOLD}Error:{C.RESET} Cannot connect to control plane at {C.CYAN}{BASE_URL}{C.RESET}')
    print(f'{C.DIM}  Detail: {error}{C.RESET}')
    print()
    print(f'  Is the server running?  {C.YELLOW}build-swarmv3 serve{C.RESET}')
    if BASE_URL != DEFAULT_URL:
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
            capture_output=True,
            text=True,
            timeout=300,
            stderr=subprocess.DEVNULL,
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
    else:
        print(f'{C.RED}Error:{C.RESET} Specify a drone sub-command: audit, deploy, create')
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
    cp_url = os.environ.get('SWARMV3_URL', BASE_URL)

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


def cmd_bootstrap_script(args):
    """Print the drone bootstrap script."""
    resp = api_get('/api/v1/provision/bootstrap')
    if resp is None:
        # The bootstrap endpoint returns text/plain, not JSON
        # Use raw urllib to fetch it
        url = f'{BASE_URL}/api/v1/provision/bootstrap'
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
    """Simple live status display, refreshing every 5 seconds."""
    interval = args.interval if hasattr(args, 'interval') and args.interval else 5

    print(f'{C.DIM}Monitoring... (Ctrl+C to stop, refresh every {interval}s){C.RESET}')

    try:
        while True:
            # Clear screen
            print('\033[2J\033[H', end='')

            now_str = datetime.now().strftime('%H:%M:%S')
            print(f'{C.BOLD}{C.BCYAN}Build Swarm v3 Monitor{C.RESET}  '
                  f'{C.DIM}{now_str}{C.RESET}')
            print(f'{C.DIM}{"="*70}{C.RESET}')

            try:
                data = api_get('/api/v1/status')
            except SystemExit:
                # api_get calls sys.exit on connection error — catch it here
                print(f'\n{C.RED}Server unreachable. Retrying in {interval}s...{C.RESET}')
                time.sleep(interval)
                continue

            paused = data.get('paused', False)
            if paused:
                print(f'\n  {C.BOLD}{C.BYELLOW}*** PAUSED ***{C.RESET}')

            # Queue bar
            needed    = data.get('needed', 0)
            delegated = data.get('delegated', 0)
            received  = data.get('received', 0)
            blocked   = data.get('blocked', 0)
            failed    = data.get('failed', 0)
            total     = data.get('total', 0)

            print(f'\n  {C.BOLD}Queue:{C.RESET}')
            print(f'    {C.YELLOW}Needed:    {needed:>5}{C.RESET}  '
                  f'{C.BCYAN}Delegated: {delegated:>5}{C.RESET}  '
                  f'{C.GREEN}Received:  {received:>5}{C.RESET}  '
                  f'{C.RED}Blocked:   {blocked:>5}{C.RESET}')

            if total > 0:
                pct = received / total * 100
                bar_width = 50
                filled = int(bar_width * received / total)
                bar = f'{C.BGREEN}{"#" * filled}{C.DIM}{"." * (bar_width - filled)}{C.RESET}'
                print(f'\n    [{bar}] {pct:.1f}%  ({received}/{total})')

            # Drones
            drones = data.get('drones', {})
            if drones:
                online = sum(1 for d in drones.values() if d.get('status') == 'online')
                print(f'\n  {C.BOLD}Fleet:{C.RESET} '
                      f'{C.BGREEN}{online}{C.RESET}/{len(drones)} online')

                # Active builds
                active = [(did, d) for did, d in drones.items() if d.get('current_task')]
                idle = [(did, d) for did, d in drones.items()
                        if d.get('status') == 'online' and not d.get('current_task')]

                if active:
                    print(f'\n  {C.BOLD}Building:{C.RESET}')
                    for did, d in active:
                        name = d.get('name', did[:12])
                        task = d.get('current_task', '-')
                        metrics = d.get('metrics', {})
                        cpu = metrics.get('cpu_percent')
                        load = metrics.get('load_1m')
                        extra = ''
                        if cpu is not None:
                            extra = f'  cpu:{cpu:.0f}%'
                        if load is not None:
                            extra += f'  load:{load:.1f}'
                        print(f'    {C.BCYAN}{name:18s}{C.RESET} {task}'
                              f'{C.DIM}{extra}{C.RESET}')

                if idle:
                    names = [d.get('name', did[:12]) for did, d in idle]
                    print(f'\n  {C.DIM}Idle: {", ".join(names)}{C.RESET}')

            # Timing
            timing = data.get('timing', {})
            if timing and timing.get('total_builds', 0) > 0:
                rate = timing.get('success_rate', 0)
                avg = fmt_duration(timing.get('avg_duration_s'))
                total_t = fmt_duration(timing.get('total_duration_s'))
                print(f'\n  {C.DIM}Stats: {timing["total_builds"]} builds, '
                      f'{rate}% success, avg {avg}, total {total_t}{C.RESET}')

            print(f'\n{C.DIM}Press Ctrl+C to exit{C.RESET}')

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f'\n{C.DIM}Monitor stopped.{C.RESET}')


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
    elif args.command == 'drone':
        cmd_drone(args)
    elif args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
