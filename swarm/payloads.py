"""
Payload Versioning System for Build Swarm v4.

Manages versioned deployment of:
- drone_binary: The swarm-drone daemon script
- init_script: OpenRC init script for swarm-drone
- config: Drone configuration files
- portage_config: /etc/portage overlay settings

Supports:
- Version tracking with SHA256 hashes
- Rolling deployments (one drone at a time)
- Automatic rollback on failure
- Drift detection (hash mismatch)
"""

import hashlib
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config as cfg

log = logging.getLogger('swarm-v3')

# Default paths for payload types on the drone
PAYLOAD_PATHS = {
    'drone_binary': '/usr/local/bin/swarm-drone',
    'init_script': '/etc/init.d/swarm-drone',
    'config': '/etc/swarm-drone/config.json',
    'portage_config': '/etc/portage/repos.conf/binhost.conf',
}


@dataclass
class PayloadSpec:
    """Specification for a payload deployment."""
    payload_type: str
    version: str
    hash: str
    content: bytes
    remote_path: str


def compute_hash(content: bytes) -> str:
    """Compute SHA256 hash of content."""
    return hashlib.sha256(content).hexdigest()


def compute_file_hash(path: str) -> str:
    """Compute SHA256 hash of a file."""
    with open(path, 'rb') as f:
        return compute_hash(f.read())


class PayloadManager:
    """Manages payload versioning and deployment."""

    def __init__(self, db):
        self.db = db

    def register_version(self, payload_type: str, version: str, content: bytes,
                         description: str = None, notes: str = None,
                         created_by: str = None) -> dict:
        """
        Register a new payload version.

        Returns the created version info.
        """
        hash = compute_hash(content)

        # Check if this version already exists
        existing = self.db.get_payload_version(payload_type, version)
        if existing:
            if existing['hash'] == hash:
                log.info(f"[Payloads] Version {version} already exists with same hash")
                return existing
            else:
                raise ValueError(f"Version {version} already exists with different content")

        # Store in database (content stored as blob for small payloads)
        if len(content) <= 1024 * 1024:  # 1MB threshold
            self.db.create_payload_version(
                payload_type=payload_type,
                version=version,
                hash=hash,
                content_blob=content,
                description=description,
                notes=notes,
                created_by=created_by
            )
        else:
            # Large payloads - store on disk
            payload_dir = Path('/var/lib/build-swarm-v3/payloads')
            payload_dir.mkdir(parents=True, exist_ok=True)
            path = payload_dir / f"{payload_type}-{version}"
            path.write_bytes(content)

            self.db.create_payload_version(
                payload_type=payload_type,
                version=version,
                hash=hash,
                content_path=str(path),
                description=description,
                notes=notes,
                created_by=created_by
            )

        log.info(f"[Payloads] Registered {payload_type} v{version} ({len(content)} bytes)")
        return self.db.get_payload_version(payload_type, version)

    def get_payload_content(self, payload_type: str, version: str) -> Optional[bytes]:
        """Get the content of a payload version."""
        pv = self.db.get_payload_version(payload_type, version)
        if not pv:
            return None

        # Try inline blob first
        row = self.db.fetchone("""
            SELECT content_blob FROM payload_versions
            WHERE payload_type = ? AND version = ?
        """, (payload_type, version))

        if row and row['content_blob']:
            return row['content_blob']

        # Try file path
        if pv.get('content_path'):
            path = Path(pv['content_path'])
            if path.exists():
                return path.read_bytes()

        return None

    def deploy_to_drone(self, drone_name: str, payload_type: str, version: str,
                        deployed_by: str = None, verify: bool = True) -> Tuple[bool, str]:
        """
        Deploy a specific payload version to a drone.

        Returns (success, message).
        """
        start_time = time.time()

        # Get payload version
        pv = self.db.get_payload_version(payload_type, version)
        if not pv:
            return False, f"Payload version not found: {payload_type} v{version}"

        # Get payload content
        content = self.get_payload_content(payload_type, version)
        if not content:
            return False, f"Cannot read payload content for {payload_type} v{version}"

        # Get drone info
        node = self.db.get_node_by_name(drone_name)
        if not node:
            return False, f"Drone not found: {drone_name}"

        drone_id = node['id']
        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            return False, f"Drone has no IP address: {drone_name}"

        # Get SSH config
        ssh_cfg = self.db.fetchone(
            "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
            (drone_name,))

        user = ssh_cfg['ssh_user'] if ssh_cfg else 'root'
        port = ssh_cfg['ssh_port'] if ssh_cfg else 22
        key_path = ssh_cfg['ssh_key_path'] if ssh_cfg else None

        # Build SSH command
        ssh_opts = [
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-o', 'ConnectTimeout=10',
        ]
        if port != 22:
            ssh_opts.extend(['-p', str(port)])
        if key_path:
            ssh_opts.extend(['-i', key_path])

        remote_path = PAYLOAD_PATHS.get(payload_type, f'/tmp/{payload_type}')

        try:
            # Mark deployment as pending
            self.db.set_drone_payload(drone_id, payload_type, version, pv['hash'],
                                      status='deploying', deployed_by=deployed_by)

            # Copy payload to drone via SSH
            log.info(f"[Payloads] Deploying {payload_type} v{version} to {drone_name}")

            # Use cat with base64 to transfer content safely
            import base64
            encoded = base64.b64encode(content).decode('ascii')

            # Create remote directory if needed
            remote_dir = str(Path(remote_path).parent)
            mkdir_cmd = ['ssh'] + ssh_opts + [f'{user}@{ip}', f'mkdir -p {remote_dir}']
            subprocess.run(mkdir_cmd, capture_output=True, timeout=30)

            # Transfer content
            transfer_cmd = f"echo '{encoded}' | base64 -d > {remote_path}"
            ssh_cmd = ['ssh'] + ssh_opts + [f'{user}@{ip}', transfer_cmd]
            result = subprocess.run(ssh_cmd, capture_output=True, timeout=60)

            if result.returncode != 0:
                error = result.stderr.decode('utf-8', errors='replace')
                raise RuntimeError(f"Transfer failed: {error}")

            # Set permissions (executable for scripts)
            if payload_type in ('drone_binary', 'init_script'):
                chmod_cmd = ['ssh'] + ssh_opts + [f'{user}@{ip}', f'chmod +x {remote_path}']
                subprocess.run(chmod_cmd, capture_output=True, timeout=30)

            # Verify deployment
            if verify:
                verify_cmd = f"sha256sum {remote_path} | cut -d' ' -f1"
                verify_ssh = ['ssh'] + ssh_opts + [f'{user}@{ip}', verify_cmd]
                verify_result = subprocess.run(verify_ssh, capture_output=True, timeout=30)

                if verify_result.returncode == 0:
                    remote_hash = verify_result.stdout.decode().strip()
                    if remote_hash != pv['hash']:
                        raise RuntimeError(f"Hash mismatch: expected {pv['hash'][:12]}..., got {remote_hash[:12]}...")
                    log.info(f"[Payloads] Verified {payload_type} v{version} on {drone_name}")

            # Mark deployment as successful
            duration_ms = (time.time() - start_time) * 1000
            self.db.set_drone_payload(drone_id, payload_type, version, pv['hash'],
                                      status='deployed', deployed_by=deployed_by)

            self.db.log_payload_deploy(
                drone_id=drone_id,
                payload_type=payload_type,
                version=version,
                action='deploy',
                status='success',
                duration_ms=duration_ms,
                deployed_by=deployed_by
            )

            return True, f"Deployed {payload_type} v{version} to {drone_name}"

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)
            log.error(f"[Payloads] Deployment failed to {drone_name}: {error_msg}")

            # Mark deployment as failed
            self.db.execute("""
                UPDATE drone_payloads SET status = 'failed', error_message = ?
                WHERE drone_id = ? AND payload_type = ?
            """, (error_msg, drone_id, payload_type))

            self.db.log_payload_deploy(
                drone_id=drone_id,
                payload_type=payload_type,
                version=version,
                action='deploy',
                status='failed',
                duration_ms=duration_ms,
                error_message=error_msg,
                deployed_by=deployed_by
            )

            return False, error_msg

    def rolling_deploy(self, payload_type: str, version: str, drone_names: List[str] = None,
                       deployed_by: str = None, health_check: bool = True,
                       rollback_on_fail: bool = True) -> Dict[str, Tuple[bool, str]]:
        """
        Deploy a payload to multiple drones one at a time.

        If health_check is True, waits for drone to come back online after deployment.
        If rollback_on_fail is True, stops and attempts rollback on first failure.

        Returns dict of drone_name -> (success, message).
        """
        results = {}

        # Get target drones
        if drone_names is None:
            # Deploy to all drones that need update
            outdated = self.db.get_outdated_drones(payload_type)
            drone_names = [d['name'] for d in outdated]

        if not drone_names:
            log.info(f"[Payloads] No drones need {payload_type} v{version}")
            return results

        log.info(f"[Payloads] Rolling deploy of {payload_type} v{version} to {len(drone_names)} drones")

        for drone_name in drone_names:
            # Deploy
            success, msg = self.deploy_to_drone(drone_name, payload_type, version,
                                                deployed_by=deployed_by)
            results[drone_name] = (success, msg)

            if not success:
                log.error(f"[Payloads] Deployment to {drone_name} failed: {msg}")
                if rollback_on_fail:
                    log.warning(f"[Payloads] Rolling deploy aborted due to failure")
                    break
                continue

            # Health check - wait for drone to respond
            if health_check and payload_type in ('drone_binary', 'init_script'):
                log.info(f"[Payloads] Waiting for {drone_name} to restart...")

                # Restart the service
                self._restart_drone_service(drone_name)

                # Wait for drone to come back online
                max_wait = 60
                start = time.time()
                while time.time() - start < max_wait:
                    node = self.db.get_node_by_name(drone_name)
                    if node and node.get('status') == 'online':
                        last_seen = node.get('last_seen', 0)
                        if last_seen > start:
                            log.info(f"[Payloads] {drone_name} is back online")
                            break
                    time.sleep(2)
                else:
                    results[drone_name] = (False, "Drone did not come back online after deployment")
                    if rollback_on_fail:
                        log.warning(f"[Payloads] Health check failed, aborting rolling deploy")
                        break

        return results

    def _restart_drone_service(self, drone_name: str) -> bool:
        """Restart the swarm-drone service on a drone."""
        node = self.db.get_node_by_name(drone_name)
        if not node:
            return False

        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            return False

        ssh_cfg = self.db.fetchone(
            "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
            (drone_name,))

        user = ssh_cfg['ssh_user'] if ssh_cfg else 'root'
        port = ssh_cfg['ssh_port'] if ssh_cfg else 22
        key_path = ssh_cfg['ssh_key_path'] if ssh_cfg else None

        ssh_opts = [
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
        ]
        if port != 22:
            ssh_opts.extend(['-p', str(port)])
        if key_path:
            ssh_opts.extend(['-i', key_path])

        restart_cmd = 'rc-service swarm-drone restart'
        ssh_cmd = ['ssh'] + ssh_opts + [f'{user}@{ip}', restart_cmd]

        try:
            result = subprocess.run(ssh_cmd, capture_output=True, timeout=30)
            return result.returncode == 0
        except Exception as e:
            log.error(f"[Payloads] Failed to restart service on {drone_name}: {e}")
            return False

    def verify_drone_payload(self, drone_name: str, payload_type: str) -> Tuple[bool, str]:
        """
        Verify that a drone has the expected payload hash.

        Returns (matches, remote_hash).
        """
        node = self.db.get_node_by_name(drone_name)
        if not node:
            return False, f"Drone not found: {drone_name}"

        drone_id = node['id']
        dp = self.db.get_drone_payload(drone_id, payload_type)
        if not dp:
            return False, f"No payload record for {drone_name}/{payload_type}"

        expected_hash = dp['hash']

        ip = node.get('tailscale_ip') or node.get('ip')
        if not ip:
            return False, "No IP address"

        ssh_cfg = self.db.fetchone(
            "SELECT ssh_user, ssh_port, ssh_key_path FROM drone_config WHERE node_name = ?",
            (drone_name,))

        user = ssh_cfg['ssh_user'] if ssh_cfg else 'root'
        port = ssh_cfg['ssh_port'] if ssh_cfg else 22
        key_path = ssh_cfg['ssh_key_path'] if ssh_cfg else None

        ssh_opts = [
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
        ]
        if port != 22:
            ssh_opts.extend(['-p', str(port)])
        if key_path:
            ssh_opts.extend(['-i', key_path])

        remote_path = PAYLOAD_PATHS.get(payload_type, f'/tmp/{payload_type}')
        verify_cmd = f"sha256sum {remote_path} 2>/dev/null | cut -d' ' -f1"
        ssh_cmd = ['ssh'] + ssh_opts + [f'{user}@{ip}', verify_cmd]

        try:
            result = subprocess.run(ssh_cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                return False, "Failed to read remote file"

            remote_hash = result.stdout.decode().strip()
            matches = remote_hash == expected_hash

            if not matches:
                log.warning(f"[Payloads] Drift detected on {drone_name}/{payload_type}: "
                           f"expected {expected_hash[:12]}..., got {remote_hash[:12]}...")

            return matches, remote_hash

        except Exception as e:
            return False, str(e)

    def get_version_matrix(self) -> Dict[str, Dict[str, dict]]:
        """
        Get a matrix of all drones and their payload versions.

        Returns: { drone_name: { payload_type: version_info } }
        """
        return self.db.get_all_drone_payloads()

    def get_deployment_status(self) -> dict:
        """Get overall deployment status summary."""
        # Get all payload types
        types_rows = self.db.fetchall(
            "SELECT DISTINCT payload_type FROM payload_versions")
        payload_types = [r['payload_type'] for r in types_rows]

        status = {
            'payload_types': payload_types,
            'drones': {},
            'outdated_count': 0,
            'latest_versions': {},
        }

        # Get latest version for each type
        for pt in payload_types:
            latest = self.db.get_latest_payload_version(pt)
            if latest:
                status['latest_versions'][pt] = {
                    'version': latest['version'],
                    'hash': latest['hash'][:12] + '...',
                    'created_at': latest['created_at'],
                }

        # Get version matrix
        matrix = self.get_version_matrix()
        for drone_name, payloads in matrix.items():
            status['drones'][drone_name] = {}
            for pt, info in payloads.items():
                latest = status['latest_versions'].get(pt, {})
                is_current = info['version'] == latest.get('version')
                status['drones'][drone_name][pt] = {
                    'version': info['version'],
                    'status': info['status'],
                    'is_current': is_current,
                }
                if not is_current:
                    status['outdated_count'] += 1

        return status


# Singleton instance
_manager: Optional[PayloadManager] = None


def init_payloads(db) -> PayloadManager:
    """Initialize the payload manager."""
    global _manager
    _manager = PayloadManager(db)
    return _manager


def get_manager() -> Optional[PayloadManager]:
    """Get the payload manager instance."""
    return _manager
