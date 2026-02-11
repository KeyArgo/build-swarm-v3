"""
Drone health monitoring and circuit breaker for Build Swarm v3.

v3.1: Added SSH-based health probing, service restart escalation,
and escalation ladder (restart → reboot → manual intervention).
"""

import json
import logging
import subprocess
import threading
import time
from typing import Optional

from . import config as cfg
from .events import add_event

log = logging.getLogger('swarm-v3')


class DroneHealthMonitor:
    """Circuit breaker and health tracking for drones."""

    def __init__(self, db):
        self.db = db

    def check_grounded(self, drone_id: str, drone_ip: str = None) -> bool:
        """Check if a drone is grounded (too many failures).

        v3.1 escalation ladder:
        1. First grounding: restart service via SSH (less destructive)
        2. Second grounding (already rebooted=0): full reboot via SSH
        3. After reboot: if still failing, ground indefinitely

        Returns True if grounded (should NOT receive work).
        """
        health = self.db.get_drone_health(drone_id)
        failures = health.get('failures', 0)

        if failures < cfg.MAX_DRONE_FAILURES:
            return False

        # Check if grounding period has expired
        grounded_until = health.get('grounded_until')
        now = time.time()

        if grounded_until and now >= grounded_until:
            # Cool-off period expired — reset
            self.db.reset_drone_health(drone_id)
            drone_name = self.db.get_drone_name(drone_id)
            log.info(f"[UNGROUND] {drone_name} - cool-off period expired, reset failures")
            return False

        if not grounded_until:
            # First time hitting ground threshold — set grounding period
            until = now + (cfg.GROUNDING_TIMEOUT_MINUTES * 60)
            self.db.ground_drone(drone_id, until)
            drone_name = self.db.get_drone_name(drone_id)
            log.error(f"[GROUNDED] {drone_name} - {failures} failures, grounded for {cfg.GROUNDING_TIMEOUT_MINUTES}m")
            add_event('grounded', f"{drone_name} grounded ({failures} failures, {cfg.GROUNDING_TIMEOUT_MINUTES}m cooldown)",
                      {'drone': drone_name, 'failures': failures, 'timeout_min': cfg.GROUNDING_TIMEOUT_MINUTES})

            # Reclaim all work from grounded drone
            self._reclaim_drone_work(drone_id)

            # v3.1 Escalation ladder
            if drone_ip:
                rebooted = health.get('rebooted', 0)
                if not rebooted:
                    # First escalation: try service restart (less destructive)
                    self.restart_drone_service(drone_id, drone_ip)
                else:
                    # Already tried restart, escalate to full reboot
                    self._try_reboot(drone_id, drone_ip)

        return True

    def record_success(self, drone_id: str):
        """Record a successful build — resets circuit breaker."""
        self.db.reset_drone_health(drone_id)

    def record_failure(self, drone_id: str) -> dict:
        """Record a build failure — trips circuit breaker."""
        return self.db.record_drone_failure(drone_id)

    def _reclaim_drone_work(self, drone_id: str):
        """Reclaim all delegated packages from a grounded drone."""
        packages = self.db.get_delegated_packages(drone_id)
        drone_name = self.db.get_drone_name(drone_id)

        for pkg in packages:
            self.db.reclaim_package(pkg['package'])
            log.warning(f"[RECLAIM] {pkg['package']} from grounded {drone_name}")

        if packages:
            log.info(f"[GROUNDED] Reclaimed {len(packages)} packages from {drone_name}")

    def _build_ssh_cmd(self, drone_ip: str, drone_name: str = None, remote_cmd: str = '') -> list:
        """Build an SSH command list using per-drone config from drone_config table."""
        ssh_cfg = self.db.get_ssh_config(drone_name) if drone_name else {}
        user = ssh_cfg.get('user') or 'root'
        port = ssh_cfg.get('port') or 22
        key_path = ssh_cfg.get('key_path')

        cmd = ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no']
        if port != 22:
            cmd += ['-p', str(port)]
        if key_path:
            cmd += ['-i', key_path]
        cmd.append(f'{user}@{drone_ip}')
        if remote_cmd:
            cmd.append(remote_cmd)
        return cmd

    def restart_drone_service(self, drone_id: str, drone_ip: str):
        """Restart the swarm-drone service via SSH (less destructive than reboot).

        Uses OpenRC: rc-service swarm-drone restart
        """
        if drone_ip in cfg.PROTECTED_HOSTS:
            return

        drone_name = self.db.get_drone_name(drone_id)

        def run_restart():
            try:
                log.warning(f"[RESTART] Restarting swarm-drone on {drone_name} ({drone_ip})")
                cmd = self._build_ssh_cmd(
                    drone_ip, drone_name,
                    'rc-service swarm-drone restart 2>&1 || /opt/build-swarm/bin/swarm-drone &'
                )
                result = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
                if result.returncode == 0:
                    log.info(f"[RESTART] {drone_name} service restarted successfully")
                    add_event('control', f"{drone_name} service restarted via SSH",
                              {'drone': drone_name, 'ip': drone_ip})
                else:
                    log.error(f"[RESTART] {drone_name} restart failed: {result.stderr[:200]}")
            except Exception as e:
                log.error(f"[RESTART] Failed for {drone_name}: {e}")

        threading.Thread(target=run_restart, daemon=True).start()
        # Mark as rebooted=1 so next grounding escalates to full reboot
        self.db.mark_drone_rebooted(drone_id)

    def _try_reboot(self, drone_id: str, drone_ip: str):
        """Attempt to reboot a drone (safety-checked)."""
        if drone_ip in cfg.PROTECTED_HOSTS:
            drone_name = self.db.get_drone_name(drone_id)
            log.error(f"BLOCKED: Refusing to reboot protected host {drone_ip} ({drone_name})")
            return

        # Check capabilities
        node = self.db.get_node(drone_id)
        if node:
            caps = node.get('capabilities', {})
            if not caps.get('auto_reboot', True):
                return

        drone_name = self.db.get_drone_name(drone_id)

        def run_reboot():
            try:
                log.warning(f"[REBOOT] Attempting reboot of {drone_name} ({drone_ip})")
                cmd = self._build_ssh_cmd(drone_ip, drone_name, 'reboot')
                subprocess.run(cmd, timeout=10, capture_output=True)
            except Exception as e:
                log.error(f"[REBOOT] Failed for {drone_name}: {e}")

        threading.Thread(target=run_reboot, daemon=True).start()
        add_event('control', f"{drone_name} rebooted via SSH (escalation)",
                  {'drone': drone_name, 'ip': drone_ip})
        log.warning(f"[REBOOT] Triggered for {drone_name} ({drone_ip})")

    def probe_drone_health(self, drone_id: str, drone_ip: str) -> dict:
        """SSH-based health probe: check process, load, disk, stuck emerge.

        Returns dict with probe results. Non-blocking (runs inline).
        """
        if not drone_ip or drone_ip in cfg.PROTECTED_HOSTS:
            return {'status': 'skipped', 'reason': 'protected or no IP'}

        drone_name = self.db.get_drone_name(drone_id)
        result = {
            'drone': drone_name,
            'ip': drone_ip,
            'timestamp': time.time(),
            'checks': {},
        }

        try:
            # Single SSH call to check multiple things at once
            cmd = (
                "echo PROC=$(pgrep -c -f swarm-drone 2>/dev/null || echo 0);"
                "echo LOAD=$(cat /proc/loadavg | cut -d' ' -f1);"
                "echo DISK=$(df /var/cache 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%');"
                "echo EMERGE=$(pgrep -c -f 'emerge.*ebuild' 2>/dev/null || echo 0);"
                "echo UPTIME=$(cat /proc/uptime | cut -d' ' -f1)"
            )
            ssh_cmd = self._build_ssh_cmd(drone_ip, drone_name, cmd)
            proc = subprocess.run(ssh_cmd, timeout=15, capture_output=True, text=True)

            if proc.returncode != 0:
                result['status'] = 'unreachable'
                result['error'] = proc.stderr[:200]
                return result

            # Parse output
            for line in proc.stdout.strip().split('\n'):
                if '=' in line:
                    key, val = line.split('=', 1)
                    result['checks'][key.strip()] = val.strip()

            result['status'] = 'ok'

            # Analyze results
            procs = int(result['checks'].get('PROC', '0'))
            load = float(result['checks'].get('LOAD', '0'))
            disk = int(result['checks'].get('DISK', '0') or '0')

            if procs == 0:
                result['status'] = 'service_down'
                log.warning(f"[PROBE] {drone_name}: swarm-drone service NOT running")
            if load > 20:
                result['status'] = 'overloaded'
                log.warning(f"[PROBE] {drone_name}: load {load} (very high)")
            if disk > 90:
                result['status'] = 'disk_full'
                log.warning(f"[PROBE] {drone_name}: disk {disk}% full")

        except subprocess.TimeoutExpired:
            result['status'] = 'timeout'
        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        # Store probe result in drone_health table
        try:
            self.db.execute("""
                INSERT INTO drone_health (node_id, failures, last_failure)
                VALUES (?, 0, NULL)
                ON CONFLICT(node_id) DO UPDATE SET node_id = node_id
            """, (drone_id,))
            # Update probe columns if they exist
            try:
                self.db.execute("""
                    UPDATE drone_health SET last_probe_result = ?, last_probe_at = ?
                    WHERE node_id = ?
                """, (json.dumps(result), time.time(), drone_id))
            except Exception:
                pass  # Columns may not exist yet (pre-migration)
        except Exception:
            pass

        return result

    def unground_all(self) -> int:
        """Unground all drones."""
        self.db.reset_drone_health()
        log.info("[UNGROUND] All drones manually ungrounded")
        return 1

    def unground_drone(self, drone_id: str):
        """Unground a specific drone."""
        self.db.reset_drone_health(drone_id)
        drone_name = self.db.get_drone_name(drone_id)
        log.info(f"[UNGROUND] {drone_name} manually ungrounded")
