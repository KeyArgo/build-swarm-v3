"""
Drone health monitoring and circuit breaker for Build Swarm v3.

Ported from swarm-orchestrator's drone_health logic.
"""

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

        Returns True if grounded (should NOT receive work).
        Handles grounding timeout and auto-reboot.
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

            # Auto-reboot if eligible
            if drone_ip and not health.get('rebooted'):
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
                subprocess.run(
                    ['ssh', '-o', 'ConnectTimeout=5',
                     '-o', 'StrictHostKeyChecking=no',
                     f'root@{drone_ip}', 'reboot'],
                    timeout=10, capture_output=True
                )
            except Exception as e:
                log.error(f"[REBOOT] Failed for {drone_name}: {e}")

        threading.Thread(target=run_reboot, daemon=True).start()
        self.db.mark_drone_rebooted(drone_id)
        log.warning(f"[REBOOT] Triggered for {drone_name} ({drone_ip})")

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
