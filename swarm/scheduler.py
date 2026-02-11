"""
Work scheduler for Build Swarm v3.

Ported from swarm-orchestrator's get_work_for_drone() logic.
All state access via SQLite instead of in-memory dicts.
"""

import logging
import time
from typing import Optional, Any

from . import config as cfg
from .db import SwarmDB
from .events import add_event
from .health import DroneHealthMonitor

log = logging.getLogger('swarm-v3')


class Scheduler:
    """Assigns work to drones based on capacity, health, and priority."""

    def __init__(self, db: SwarmDB, health: DroneHealthMonitor):
        self.db = db
        self.health = health
        # Track packages rebalanced away from drones (drone_id -> set of packages)
        self._rebalanced = {}  # type: dict[str, set[str]]

    def is_valid_assignment(self, drone_id: str, package: str) -> bool:
        """Check if a package is currently assigned to this drone.

        Used to filter stale completions from v2 agents that keep building
        packages after rebalancing has reassigned them.
        """
        return self.db.is_package_assigned_to(package, drone_id)

    def get_stale_assignments(self, drone_id: str) -> list:
        """Get packages that were rebalanced away from this drone.

        Returns list of package names the drone should stop building.
        Clears the tracking after returning (one-shot notification).
        """
        stale = list(self._rebalanced.pop(drone_id, set()))
        return stale

    def get_work(self, drone_id: str, drone_ip: str = None) -> Optional[Any]:
        """Get next package for a drone to build.

        Returns:
            - str: package atom (e.g., "=dev-qt/qtbase-6.10.1")
            - dict: directive (e.g., {"action": "sync_portage", ...})
            - None: no work available
        """
        drone_name = self.db.get_drone_name(drone_id)
        log.debug(f"get_work: drone={drone_name} ({drone_id[:12]})")

        # Check if orchestrator is paused
        paused = self.db.get_config('paused', 'false')
        if paused == 'true':
            log.debug(f"Paused, no work for {drone_name}")
            return None

        # Portage sync check
        expected_ts = self.db.get_config('expected_portage_timestamp')
        if expected_ts:
            node = self.db.get_node(drone_id)
            if node:
                drone_ts = node.get('capabilities', {}).get('portage_timestamp')
                if drone_ts != expected_ts:
                    log.warning(f"[STALE] {drone_name} has stale portage "
                               f"(has: {drone_ts}, expected: {expected_ts})")
                    return {"action": "sync_portage",
                            "expected_timestamp": expected_ts}

        # Circuit breaker check
        if self.health.check_grounded(drone_id, drone_ip):
            return None

        # Upload capability check — don't assign work to drones that can't deliver
        if self.health.is_upload_impaired(drone_id):
            drone_name_log = self.db.get_drone_name(drone_id)
            log.debug(f"Upload-impaired, no work for {drone_name_log}")
            return None

        # Check existing assignments
        assigned = self.db.get_delegated_packages(drone_id)
        if assigned:
            return assigned[0]['package']

        # Determine if sweeper
        is_sweeper = drone_name.lower().startswith(cfg.SWEEPER_PREFIX.lower())

        if is_sweeper:
            return self._assign_sweeper_work(drone_id, drone_name)
        else:
            return self._assign_regular_work(drone_id, drone_name, drone_ip)

    def _assign_regular_work(self, drone_id: str, drone_name: str,
                              drone_ip: str = None) -> Optional[str]:
        """Assign work to a regular drone."""
        # Calculate queue target
        node = self.db.get_node(drone_id)
        cores = 0
        if node:
            cores = node.get('cores') or node.get('capabilities', {}).get('cores', 0)

        if cores > 0:
            queue_target = max(1, cores // cfg.CORES_PER_SLOT)
        else:
            queue_target = cfg.QUEUE_TARGET

        # Get needed packages (fetch extra to have alternatives if some are skipped)
        needed = self.db.get_needed_packages(limit=queue_target * 3)

        if not needed:
            # Try auto-balance (steal from overloaded drones)
            stolen = self._auto_balance(drone_id, drone_name, queue_target, cores)
            if stolen:
                assigned = self.db.get_delegated_packages(drone_id)
                return assigned[0]['package'] if assigned else None
            return None

        # Assign packages up to queue target, skipping packages this drone
        # has previously failed (prevents same-drone-same-failure loops)
        first_package = None
        assigned_count = 0
        skipped = 0

        for pkg_row in needed:
            if assigned_count >= queue_target:
                break

            package = pkg_row['package']
            queue_id = pkg_row['id']

            # v3.1: Skip packages this drone has already failed
            if self.db.has_drone_failed_package(drone_id, package):
                skipped += 1
                log.debug(f"[SKIP] {package} — {drone_name} previously failed this")
                continue

            if self.db.assign_package(queue_id, drone_id):
                if first_package is None:
                    first_package = package
                assigned_count += 1
                log.info(f"[ASSIGN] {package} -> {drone_name}")

        if skipped > 0:
            log.info(f"[ASSIGN] {drone_name}: skipped {skipped} previously-failed packages")

        if assigned_count > 0:
            log.info(f"[QUEUE] {drone_name}: {assigned_count} pkgs assigned "
                     f"(target={queue_target}, cores={cores})")
            add_event('assign', f"{assigned_count} packages assigned to {drone_name}",
                      {'drone': drone_name, 'count': assigned_count,
                       'first_package': first_package, 'target': queue_target})

        return first_package

    def _assign_sweeper_work(self, drone_id: str,
                              drone_name: str) -> Optional[str]:
        """Assign blocked packages to a sweeper drone."""
        blocked = self.db.get_blocked_packages()
        if not blocked:
            log.debug(f"[SWEEPER] {drone_name}: no blocked packages")
            return None

        first_package = None
        assigned_count = 0

        # Calculate slots
        node = self.db.get_node(drone_id)
        cores = 0
        if node:
            cores = node.get('cores') or node.get('capabilities', {}).get('cores', 0)
        queue_target = max(1, cores // cfg.CORES_PER_SLOT) if cores > 0 else cfg.QUEUE_TARGET

        for pkg_row in blocked:
            if assigned_count >= queue_target:
                break

            package = pkg_row['package']
            queue_id = pkg_row['id']

            # Unblock and assign
            self.db.execute("""
                UPDATE queue SET status = 'delegated', assigned_to = ?,
                    assigned_at = ?
                WHERE id = ? AND status = 'blocked'
            """, (drone_id, time.time(), queue_id))

            if first_package is None:
                first_package = package
            assigned_count += 1
            log.info(f"[SWEEPER] {package} -> {drone_name} (last resort)")

        if assigned_count > 0:
            log.info(f"[SWEEPER] {drone_name}: {assigned_count} blocked packages assigned")

        return first_package

    def _auto_balance(self, drone_id: str, drone_name: str,
                       queue_target: int, cores: int) -> int:
        """Steal work from overloaded drones. Returns count stolen."""
        # Get all delegated packages grouped by drone
        all_delegated = self.db.get_delegated_packages()

        drone_queues = {}
        for pkg in all_delegated:
            owner = pkg['assigned_to']
            if owner not in drone_queues:
                drone_queues[owner] = []
            drone_queues[owner].append(pkg)

        # Don't steal if requesting drone already has work
        if drone_queues.get(drone_id):
            return 0

        # Find donors with >2 packages
        donors = []
        for did, pkgs in drone_queues.items():
            if len(pkgs) <= 2:
                continue
            # Check donor is online
            donor_node = self.db.get_node(did)
            if not donor_node or donor_node['status'] != 'online':
                continue
            # Skip sweeper donors
            donor_name = donor_node.get('name', '')
            if donor_name.lower().startswith(cfg.SWEEPER_PREFIX.lower()):
                continue
            donors.append((did, pkgs, donor_name))

        if not donors:
            return 0

        # Sort by queue length descending
        donors.sort(key=lambda x: len(x[1]), reverse=True)

        stolen = 0
        for donor_id, donor_pkgs, donor_name_str in donors:
            if stolen >= queue_target:
                break

            # Sort by assigned_at desc (steal newest first)
            donor_pkgs.sort(key=lambda p: p.get('assigned_at', 0), reverse=True)
            max_take = len(donor_pkgs) // 2

            taken = 0
            for pkg in donor_pkgs:
                if stolen >= queue_target or taken >= max_take:
                    break
                remaining = len(donor_pkgs) - taken
                if remaining <= 2:
                    break

                # Reassign
                self.db.execute("""
                    UPDATE queue SET assigned_to = ?, assigned_at = ?
                    WHERE id = ? AND assigned_to = ? AND status = 'delegated'
                """, (drone_id, time.time(), pkg['id'], donor_id))

                # v3.1: Track rebalanced packages so stale completions can be discarded
                if donor_id not in self._rebalanced:
                    self._rebalanced[donor_id] = set()
                self._rebalanced[donor_id].add(pkg['package'])

                stolen += 1
                taken += 1
                log.info(f"[REBALANCE] {pkg['package']}: {donor_name_str} -> {drone_name}")
                add_event('rebalance', f"{pkg['package']}: {donor_name_str} -> {drone_name}",
                          {'package': pkg['package'], 'from': donor_name_str, 'to': drone_name})

        if stolen > 0:
            log.info(f"[REBALANCE] {drone_name}: stole {stolen} packages "
                     f"(target={queue_target}, cores={cores})")

        return stolen

    def reclaim_offline_work(self, timeout_hours: int = 2):
        """Reclaim work from offline/timed-out drones."""
        cutoff = time.time() - (timeout_hours * 3600)
        delegated = self.db.get_delegated_packages()
        reclaimed = 0

        for pkg in delegated:
            drone_id = pkg['assigned_to']
            node = self.db.get_node(drone_id)

            should_reclaim = False
            reason = ""

            if not node or node['status'] != 'online':
                should_reclaim = True
                reason = "drone offline"
            elif pkg['assigned_at'] and pkg['assigned_at'] < cutoff:
                should_reclaim = True
                reason = f"build timeout (>{timeout_hours}h)"

            if should_reclaim:
                drone_name = self.db.get_drone_name(drone_id)
                self.db.reclaim_package(pkg['package'])
                log.warning(f"[RECLAIM] {pkg['package']} from {drone_name} - {reason}")
                add_event('reclaim', f"{pkg['package']} reclaimed from {drone_name} ({reason})",
                          {'package': pkg['package'], 'drone': drone_name, 'reason': reason})
                reclaimed += 1

        if reclaimed:
            log.info(f"[RECLAIM] Total {reclaimed} packages reclaimed")
        return reclaimed

    def auto_age_blocked(self):
        """Unblock packages that have been blocked longer than FAILURE_AGE_MINUTES."""
        cutoff = time.time() - (cfg.FAILURE_AGE_MINUTES * 60)

        # Find blocked packages with old enough failures
        blocked = self.db.get_blocked_packages()
        aged = 0

        for pkg in blocked:
            # Use the last history entry to determine age
            history = self.db.fetchone("""
                SELECT built_at FROM build_history
                WHERE package = ? ORDER BY built_at DESC LIMIT 1
            """, (pkg['package'],))

            if history and history['built_at'] < cutoff:
                self.db.execute("""
                    UPDATE queue SET status = 'needed', failure_count = 0,
                        assigned_to = NULL, error_message = NULL
                    WHERE id = ?
                """, (pkg['id'],))
                aged += 1
                log.info(f"[AUTO-RETRY] {pkg['package']} unblocked after {cfg.FAILURE_AGE_MINUTES}m")

        if aged:
            log.info(f"[AUTO-RETRY] {aged} packages auto-unblocked")
        return aged
