"""
Self-Healing Drone Monitor for Build Swarm v4.

Autonomous recovery system with escalation ladder:
  Level 0: Normal operation
  Level 1: Service restart (rc-service swarm-drone restart)
  Level 2: Hard restart (kill + manual start)
  Level 3: Container reboot (LXC/QEMU only - NEVER bare metal)
  Level 4: Alert admin (manual intervention required)

v4.0: Initial implementation
"""

import json
import logging
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from .events import add_event

log = logging.getLogger('swarm-v3')


# ── Drone Type Registry ─────────────────────────────────────────────────────

class DroneType:
    """Enum-like constants for drone types."""
    LXC = 'lxc'
    QEMU = 'qemu'
    BARE_METAL = 'bare-metal'
    UNKNOWN = 'unknown'


# Known drone types - can be overridden from drone_config table
_DEFAULT_DRONE_TYPES = {
    # Jove network (10.0.0.x) - typically bare metal
    'drone-io': DroneType.BARE_METAL,
    # Kronos network (192.168.50.x / 100.x.x.x) - typically LXC containers
    'dr-tb': DroneType.LXC,
    'drone-TestBed': DroneType.LXC,
    'dr-titan': DroneType.LXC,
    'drone-Trance': DroneType.LXC,
    'dr-mm2': DroneType.LXC,
    'drone-Maru': DroneType.LXC,
}


def get_drone_type(db, drone_name: str) -> str:
    """Get drone type from config or defaults.

    Check drone_config table first, fall back to defaults, then unknown.
    """
    # Check drone_config for explicit type
    row = db.fetchone(
        "SELECT metadata_json FROM drone_config WHERE node_name = ?",
        (drone_name,))
    if row and row['metadata_json']:
        try:
            meta = json.loads(row['metadata_json'])
            if 'drone_type' in meta:
                return meta['drone_type']
        except (json.JSONDecodeError, TypeError):
            pass

    # Check defaults
    if drone_name in _DEFAULT_DRONE_TYPES:
        return _DEFAULT_DRONE_TYPES[drone_name]

    return DroneType.UNKNOWN


def is_reboot_safe(db, drone_name: str) -> Tuple[bool, str]:
    """Check if a drone can be safely rebooted.

    Returns (safe, reason) tuple.
    """
    dtype = get_drone_type(db, drone_name)

    if dtype == DroneType.BARE_METAL:
        return False, "bare-metal host - reboot blocked for safety"
    elif dtype == DroneType.UNKNOWN:
        return False, "unknown drone type - reboot blocked for safety"
    elif dtype in (DroneType.LXC, DroneType.QEMU):
        return True, f"{dtype} container - safe to reboot"
    else:
        return False, f"unrecognized type '{dtype}' - reboot blocked"


# ── Escalation Ladder ───────────────────────────────────────────────────────

class EscalationLevel:
    """Escalation levels for drone recovery."""
    NORMAL = 0
    SERVICE_RESTART = 1
    HARD_RESTART = 2
    CONTAINER_REBOOT = 3
    ALERT_ADMIN = 4


ESCALATION_ACTIONS = {
    EscalationLevel.SERVICE_RESTART: ('restart_service', 30),
    EscalationLevel.HARD_RESTART: ('hard_restart', 30),
    EscalationLevel.CONTAINER_REBOOT: ('reboot_container', 120),
    EscalationLevel.ALERT_ADMIN: ('alert_admin', 0),
}


# ── Self-Healing Monitor ────────────────────────────────────────────────────

class SelfHealingMonitor:
    """Autonomous drone recovery with safe escalation.

    Probes drones periodically and escalates recovery actions when
    unhealthy conditions are detected.
    """

    def __init__(self, db, health_monitor=None):
        self.db = db
        self.health = health_monitor
        self.escalation_state: Dict[str, dict] = {}  # drone_id -> state
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._probe_interval = 30  # seconds between probes
        self._lock = threading.Lock()

    def start(self):
        """Start the self-healing monitor background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name='self-healing'
        )
        self._thread.start()
        log.info("[SELF-HEAL] Monitor started (probe interval: %ds)", self._probe_interval)

    def stop(self):
        """Stop the self-healing monitor."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("[SELF-HEAL] Monitor stopped")

    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                self._probe_all_drones()
            except Exception as e:
                log.error("[SELF-HEAL] Probe cycle error: %s", e)

            # Sleep in small intervals for responsive shutdown
            for _ in range(self._probe_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _probe_all_drones(self):
        """Probe all registered drones."""
        nodes = self.db.get_all_nodes(include_offline=True)

        for node in nodes:
            if node['type'] not in ('drone', 'sweeper'):
                continue

            # Skip paused drones
            if node.get('paused'):
                continue

            result = self._probe_drone(node)
            self._handle_probe_result(node, result)

    def _probe_drone(self, node: dict) -> dict:
        """Send a health probe to a drone via SSH."""
        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            return {'status': 'error', 'reason': 'no IP address'}

        drone_name = node['name']
        result = {
            'drone': drone_name,
            'ip': ip,
            'timestamp': time.time(),
            'checks': {},
        }

        try:
            # Build SSH command
            ssh_cmd = self._build_ssh_cmd(drone_name, ip)

            # Single SSH call to check multiple things
            check_cmd = (
                "echo PROC=$(pgrep -c -f 'swarm-drone|python.*drone' 2>/dev/null || echo 0);"
                "echo LOAD=$(cat /proc/loadavg 2>/dev/null | cut -d' ' -f1 || echo 0);"
                "echo DISK=$(df /var/cache 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%' || echo 0);"
                "echo EMERGE=$(pgrep -c -f 'emerge' 2>/dev/null || echo 0);"
                "echo UPTIME=$(cat /proc/uptime 2>/dev/null | cut -d' ' -f1 || echo 0);"
                "echo MEM=$(free -m 2>/dev/null | awk '/^Mem:/{printf \"%.0f\", $3/$2*100}' || echo 0)"
            )

            cmd = ssh_cmd + [check_cmd]
            proc = subprocess.run(cmd, timeout=15, capture_output=True, text=True)

            if proc.returncode != 0:
                result['status'] = 'unreachable'
                result['error'] = proc.stderr[:200] if proc.stderr else 'SSH failed'
                return result

            # Parse output
            for line in proc.stdout.strip().split('\n'):
                if '=' in line:
                    key, val = line.split('=', 1)
                    result['checks'][key.strip()] = val.strip()

            # Analyze results
            procs = int(result['checks'].get('PROC', '0') or '0')
            load = float(result['checks'].get('LOAD', '0') or '0')
            disk = int(result['checks'].get('DISK', '0') or '0')
            mem = int(result['checks'].get('MEM', '0') or '0')

            if procs == 0:
                result['status'] = 'service_down'
            elif load > 50:
                result['status'] = 'overloaded'
            elif disk > 95:
                result['status'] = 'disk_critical'
            elif disk > 90:
                result['status'] = 'disk_warning'
            elif mem > 95:
                result['status'] = 'memory_critical'
            else:
                result['status'] = 'ok'

        except subprocess.TimeoutExpired:
            result['status'] = 'timeout'
        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        return result

    def _build_ssh_cmd(self, drone_name: str, ip: str) -> List[str]:
        """Build SSH command with per-drone config."""
        # Check drone_config for SSH settings
        ssh_cfg = self.db.fetchone(
            "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
            (drone_name,))

        user = 'root'
        port = 22
        key_path = None

        if ssh_cfg:
            user = ssh_cfg['ssh_user'] or 'root'
            port = ssh_cfg['ssh_port'] or 22
            key_path = ssh_cfg['ssh_key_path']

        cmd = [
            'ssh',
            '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'BatchMode=yes',
            '-o', 'LogLevel=ERROR',
        ]

        if port != 22:
            cmd.extend(['-p', str(port)])
        if key_path:
            cmd.extend(['-i', key_path])

        cmd.append(f'{user}@{ip}')
        return cmd

    def _handle_probe_result(self, node: dict, result: dict):
        """Handle probe result and decide whether to escalate."""
        drone_id = node['id']
        drone_name = node['name']
        status = result.get('status', 'error')

        # Store probe result in database
        self._store_probe_result(drone_id, result)

        if status == 'ok':
            # Healthy - reset escalation state
            with self._lock:
                if drone_id in self.escalation_state:
                    old_level = self.escalation_state[drone_id].get('level', 0)
                    if old_level > 0:
                        log.info("[SELF-HEAL] %s recovered (was at level %d)", drone_name, old_level)
                        add_event('heal', f"{drone_name} recovered from escalation level {old_level}",
                                  {'drone': drone_name, 'old_level': old_level})
                    del self.escalation_state[drone_id]
            return

        # Unhealthy - check escalation state
        with self._lock:
            state = self.escalation_state.get(drone_id, {
                'level': 0,
                'last_action': 0,
                'attempts': 0,
            })

            current_level = state['level']
            last_action = state['last_action']
            now = time.time()

            # Check if we should escalate
            if current_level < EscalationLevel.ALERT_ADMIN:
                action_info = ESCALATION_ACTIONS.get(current_level + 1)
                if action_info:
                    action_name, cooldown = action_info

                    # Wait for cooldown from last action
                    if now - last_action < cooldown:
                        return

                    # Execute escalation
                    success = self._execute_escalation(node, current_level + 1, status)

                    state['level'] = current_level + 1
                    state['last_action'] = now
                    state['attempts'] = state.get('attempts', 0) + 1
                    self.escalation_state[drone_id] = state

    def _execute_escalation(self, node: dict, level: int, reason: str) -> bool:
        """Execute an escalation action."""
        drone_name = node['name']
        ip = node.get('tailscale_ip') or node.get('ip')

        if not ip:
            log.error("[SELF-HEAL] Cannot escalate %s: no IP", drone_name)
            return False

        action_info = ESCALATION_ACTIONS.get(level)
        if not action_info:
            return False

        action_name, _ = action_info

        log.warning("[SELF-HEAL] Escalating %s to level %d (%s) - reason: %s",
                    drone_name, level, action_name, reason)
        add_event('escalate', f"{drone_name} escalated to level {level} ({action_name})",
                  {'drone': drone_name, 'level': level, 'action': action_name, 'reason': reason})

        if action_name == 'restart_service':
            return self._action_restart_service(drone_name, ip)
        elif action_name == 'hard_restart':
            return self._action_hard_restart(drone_name, ip)
        elif action_name == 'reboot_container':
            return self._action_reboot_container(drone_name, ip)
        elif action_name == 'alert_admin':
            return self._action_alert_admin(drone_name, reason)

        return False

    def _action_restart_service(self, drone_name: str, ip: str) -> bool:
        """Level 1: Restart the swarm-drone service via OpenRC."""
        try:
            cmd = self._build_ssh_cmd(drone_name, ip)
            cmd.append('rc-service swarm-drone restart 2>&1 || systemctl restart swarm-drone 2>&1')

            result = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
            success = result.returncode == 0

            log.info("[SELF-HEAL] Service restart on %s: %s",
                     drone_name, 'success' if success else 'failed')
            return success
        except Exception as e:
            log.error("[SELF-HEAL] Service restart failed on %s: %s", drone_name, e)
            return False

    def _action_hard_restart(self, drone_name: str, ip: str) -> bool:
        """Level 2: Kill all drone processes and start fresh."""
        try:
            cmd = self._build_ssh_cmd(drone_name, ip)
            # Kill any existing processes, then start
            restart_cmd = (
                "pkill -9 -f 'swarm-drone|python.*drone' 2>/dev/null; "
                "sleep 2; "
                "rc-service swarm-drone start 2>&1 || "
                "/opt/build-swarm/bin/swarm-drone &"
            )
            cmd.append(restart_cmd)

            result = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
            log.info("[SELF-HEAL] Hard restart on %s completed", drone_name)
            return True
        except Exception as e:
            log.error("[SELF-HEAL] Hard restart failed on %s: %s", drone_name, e)
            return False

    def _action_reboot_container(self, drone_name: str, ip: str) -> bool:
        """Level 3: Reboot the container (LXC/QEMU only)."""
        # Safety check
        safe, reason = is_reboot_safe(self.db, drone_name)
        if not safe:
            log.error("[SELF-HEAL] BLOCKED reboot of %s: %s", drone_name, reason)
            add_event('blocked', f"Reboot blocked for {drone_name}: {reason}",
                      {'drone': drone_name, 'reason': reason})
            return False

        try:
            cmd = self._build_ssh_cmd(drone_name, ip)
            cmd.append('reboot')

            # Fire and forget - reboot will disconnect SSH
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            log.warning("[SELF-HEAL] Container reboot initiated for %s", drone_name)
            add_event('reboot', f"{drone_name} container rebooted (self-healing)",
                      {'drone': drone_name})
            return True
        except Exception as e:
            log.error("[SELF-HEAL] Container reboot failed on %s: %s", drone_name, e)
            return False

    def _action_alert_admin(self, drone_name: str, reason: str) -> bool:
        """Level 4: Alert admin - drone requires manual intervention."""
        log.critical("[SELF-HEAL] ALERT: %s requires manual intervention - %s", drone_name, reason)
        add_event('alert', f"MANUAL INTERVENTION REQUIRED: {drone_name} - {reason}",
                  {'drone': drone_name, 'reason': reason, 'severity': 'critical'})

        # TODO: Send notification (email, webhook, etc.)
        return True

    def _store_probe_result(self, drone_id: str, result: dict):
        """Store probe result in database."""
        try:
            self.db.execute("""
                INSERT INTO drone_health (node_id, failures, last_probe_result, last_probe_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    last_probe_result = excluded.last_probe_result,
                    last_probe_at = excluded.last_probe_at
            """, (drone_id, json.dumps(result), time.time()))
        except Exception as e:
            log.debug("Failed to store probe result: %s", e)

    def get_escalation_state(self, drone_id: str = None) -> dict:
        """Get current escalation state for drones."""
        with self._lock:
            if drone_id:
                return self.escalation_state.get(drone_id, {'level': 0})
            return dict(self.escalation_state)

    def reset_escalation(self, drone_id: str):
        """Manually reset escalation state for a drone."""
        with self._lock:
            if drone_id in self.escalation_state:
                del self.escalation_state[drone_id]
                log.info("[SELF-HEAL] Escalation reset for %s",
                         self.db.get_drone_name(drone_id))


# ── Proof of Life Prober ────────────────────────────────────────────────────

class ProofOfLifeProber:
    """Send explicit ping/pong probes to drones.

    Unlike passive health monitoring, this sends an active probe and
    expects a structured response with system metrics.
    """

    def __init__(self, db):
        self.db = db
        self._sequence = 0
        self._lock = threading.Lock()

    def ping(self, drone_id: str) -> dict:
        """Send a ping probe to a drone and wait for pong.

        Returns dict with:
          - seq: sequence number
          - latency_ms: round-trip time
          - status: 'ok', 'timeout', 'error'
          - metrics: dict with load, disk, mem, etc.
        """
        node = self.db.get_node(drone_id)
        if not node:
            return {'status': 'error', 'error': 'drone not found'}

        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            return {'status': 'error', 'error': 'no IP address'}

        drone_name = node['name']

        with self._lock:
            self._sequence += 1
            seq = self._sequence

        result = {
            'seq': seq,
            'drone': drone_name,
            'timestamp': time.time(),
        }

        try:
            # Build SSH command
            ssh_cfg = self.db.fetchone(
                "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
                (drone_name,))

            user = 'root'
            port = 22
            key_path = None

            if ssh_cfg:
                user = ssh_cfg['ssh_user'] or 'root'
                port = ssh_cfg['ssh_port'] or 22
                key_path = ssh_cfg['ssh_key_path']

            cmd = [
                'ssh',
                '-o', 'ConnectTimeout=5',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'BatchMode=yes',
            ]
            if port != 22:
                cmd.extend(['-p', str(port)])
            if key_path:
                cmd.extend(['-i', key_path])
            cmd.append(f'{user}@{ip}')

            # Pong command - returns key=value pairs for easy parsing
            pong_cmd = (
                "echo LOAD=$(cat /proc/loadavg 2>/dev/null | cut -d' ' -f1 || echo 0);"
                "echo DISK=$(df /var/cache 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%' || echo 0);"
                "echo MEM=$(free -m 2>/dev/null | awk '/^Mem:/{printf \"%.0f\", $3/$2*100}' || echo 0);"
                "echo EMERGE=$(pgrep -c emerge 2>/dev/null || echo 0);"
                "echo UPTIME=$(cat /proc/uptime 2>/dev/null | cut -d' ' -f1 || echo 0);"
                f"echo SEQ={seq}"
            )
            cmd.append(pong_cmd)

            start = time.time()
            proc = subprocess.run(cmd, timeout=10, capture_output=True, text=True)
            latency = (time.time() - start) * 1000

            result['latency_ms'] = round(latency, 2)

            if proc.returncode != 0:
                result['status'] = 'error'
                result['error'] = proc.stderr[:100] if proc.stderr else 'SSH failed'
                return result

            # Parse pong response (key=value pairs, one per line)
            try:
                output = proc.stdout.strip()
                metrics = {}
                for line in output.split('\n'):
                    if '=' in line:
                        k, v = line.split('=', 1)
                        metrics[k.strip()] = v.strip()

                result['status'] = 'ok'
                result['metrics'] = {
                    'load': float(metrics.get('LOAD', 0) or 0),
                    'disk_percent': int(metrics.get('DISK', 0) or 0),
                    'mem_percent': int(metrics.get('MEM', 0) or 0),
                    'emerge_running': int(metrics.get('EMERGE', 0) or 0) > 0,
                    'uptime_seconds': float(metrics.get('UPTIME', 0) or 0),
                }
            except Exception as e:
                result['status'] = 'ok'
                result['metrics'] = {}
                result['parse_error'] = str(e)

        except subprocess.TimeoutExpired:
            result['status'] = 'timeout'
            result['latency_ms'] = 10000
        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        # Update database with ping result
        self._store_ping_result(drone_id, result)

        return result

    def _store_ping_result(self, drone_id: str, result: dict):
        """Store ping result in database."""
        try:
            self.db.execute("""
                UPDATE nodes SET
                    last_seen = ?,
                    metrics_json = ?
                WHERE id = ?
            """, (time.time(), json.dumps(result.get('metrics', {})), drone_id))
        except Exception as e:
            log.debug("Failed to store ping result: %s", e)

    def ping_all(self) -> List[dict]:
        """Ping all online drones and return results."""
        results = []
        nodes = self.db.get_all_nodes(include_offline=False)

        for node in nodes:
            if node['type'] in ('drone', 'sweeper'):
                result = self.ping(node['id'])
                results.append(result)

        return results
