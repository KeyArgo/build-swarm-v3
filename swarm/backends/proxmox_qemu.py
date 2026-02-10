"""
Proxmox QEMU backend -- creates VMs on Proxmox hosts via SSH + qm commands.

Part of the build-swarm-v3 project.
"""

import json
import os
import re
import shutil
import subprocess
import time

from swarm.backends import DroneBackend, StepResult, register_backend, ssh_run, ssh_test


KNOWN_PROXMOX_HOSTS = {
    'proxmox-io':    '10.0.0.2',
    'proxmox-titan': '10.0.0.3',
}

# VMID allocation range for QEMU build drones
VMID_MIN = 200
VMID_MAX = 299


@register_backend('proxmox-qemu')
class ProxmoxQEMUBackend(DroneBackend):
    """Proxmox QEMU VM (via SSH + qm)."""

    DESCRIPTION = 'Proxmox QEMU VM (via SSH + qm)'

    def __init__(self, host=None, name=None, vmid=None, ip=None, cores=4,
                 ram_mb=4096, disk_gb=50, storage='local-lvm', bridge='vmbr0',
                 ssh_pubkey=None, **kwargs):
        self.host = self._resolve_host(host)
        self.host_alias = host  # keep original name for display
        self.name = name
        self.vmid = vmid
        self.ip = ip
        self.cores = cores
        self.ram_mb = ram_mb
        self.disk_gb = disk_gb
        self.storage = storage
        self.bridge = bridge
        self.ssh_pubkey = ssh_pubkey
        self.tarball_path = None
        self.remote_tarball = None
        self.remote_raw_disk = None

    @staticmethod
    def _resolve_host(host):
        """Resolve a friendly hostname to an IP if known."""
        if host and host in KNOWN_PROXMOX_HOSTS:
            return KNOWN_PROXMOX_HOSTS[host]
        return host

    # ------------------------------------------------------------------
    # Availability / prerequisites
    # ------------------------------------------------------------------

    @classmethod
    def probe_availability(cls):
        """Check whether any known Proxmox host is reachable and has qm."""
        for alias, ip in KNOWN_PROXMOX_HOSTS.items():
            if not ssh_test(ip):
                continue
            try:
                result = ssh_run(ip, 'which qm', timeout=10)
                if result.returncode == 0:
                    return 'available'
            except Exception:
                continue
        return 'unavailable'

    def check_prerequisites(self):
        """Verify SSH connectivity, qm availability, and storage pool."""
        if not self.host:
            return StepResult(False, 'No Proxmox host specified')

        errors = []

        # SSH connectivity
        if not ssh_test(self.host):
            return StepResult(False, 'Cannot SSH to Proxmox host %s' % self.host)

        # qm command
        try:
            result = ssh_run(self.host, 'which qm', timeout=10)
            if result.returncode != 0:
                errors.append('qm command not found on %s' % self.host)
        except Exception as exc:
            errors.append('Failed to check qm: %s' % exc)

        # Storage pool
        try:
            result = ssh_run(
                self.host,
                'pvesm status -storage %s' % self.storage,
                timeout=10,
            )
            if result.returncode != 0:
                errors.append('Storage pool %r not found on %s' % (self.storage, self.host))
        except Exception as exc:
            errors.append('Failed to check storage: %s' % exc)

        if errors:
            return StepResult(False, '; '.join(errors))
        return StepResult(True, 'Prerequisites OK on %s' % self.host)

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------

    def allocate_id(self):
        """Allocate a VMID in the 200-299 range on the Proxmox host."""
        if self.vmid is not None:
            # Verify it's not already taken
            try:
                result = ssh_run(
                    self.host,
                    'qm status %d' % self.vmid,
                    timeout=10,
                )
                if result.returncode == 0:
                    return StepResult(False, 'VMID %d already in use' % self.vmid)
            except Exception:
                pass
            return StepResult(True, 'Using specified VMID %d' % self.vmid)

        # Discover used VMIDs
        try:
            result = ssh_run(self.host, 'qm list', timeout=10)
            used_ids = set()
            if result.returncode == 0:
                for line in result.stdout.splitlines()[1:]:  # skip header
                    parts = line.split()
                    if parts:
                        try:
                            used_ids.add(int(parts[0]))
                        except ValueError:
                            continue
        except Exception as exc:
            return StepResult(False, 'Failed to list VMs: %s' % exc)

        # Find first free VMID in range
        for candidate in range(VMID_MIN, VMID_MAX + 1):
            if candidate not in used_ids:
                self.vmid = candidate
                return StepResult(True, 'Allocated VMID %d' % self.vmid)

        return StepResult(False, 'No free VMIDs in range %d-%d' % (VMID_MIN, VMID_MAX))

    # ------------------------------------------------------------------
    # Image download
    # ------------------------------------------------------------------

    def download_image(self, cache_dir):
        """Download stage3 locally and scp it to the Proxmox host."""
        from swarm.backends.stage3 import download_stage3, find_cached_stage3

        cached = find_cached_stage3(cache_dir)
        if cached:
            self.tarball_path = cached
        else:
            tarball = download_stage3(cache_dir)
            if not tarball or not os.path.isfile(tarball):
                return StepResult(False, 'Failed to download stage3 tarball')
            self.tarball_path = tarball

        # scp tarball to Proxmox host
        remote_path = '/tmp/%s' % os.path.basename(self.tarball_path)
        try:
            result = subprocess.run(
                ['scp', self.tarball_path, 'root@%s:%s' % (self.host, remote_path)],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                return StepResult(False, 'scp failed: %s' % result.stderr.strip())
        except subprocess.TimeoutExpired:
            return StepResult(False, 'scp timed out (600s)')
        except Exception as exc:
            return StepResult(False, 'scp failed: %s' % exc)

        self.remote_tarball = remote_path
        return StepResult(True, 'Stage3 uploaded to %s:%s' % (self.host, remote_path))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self):
        """Build a raw disk image on the Proxmox host, import it, and
        configure the VM via qm.

        Steps:
        1. Create and populate a raw disk image on the remote host
        2. Create a VM definition via qm create
        3. Import the disk and attach it
        """
        if not self.vmid:
            return StepResult(False, 'No VMID allocated')
        if not self.remote_tarball:
            return StepResult(False, 'No remote tarball -- call download_image() first')
        if not self.name:
            self.name = 'drone-%d' % self.vmid

        raw_disk = '/tmp/%s.raw' % self.name
        self.remote_raw_disk = raw_disk
        disk_size_bytes = self.disk_gb * 1024 * 1024 * 1024
        mount_point = '/tmp/gentoo-stage3-mount'

        # Build the sequence of commands to run on the Proxmox host.
        # Each step is (description, command, timeout).
        disk_steps = [
            ('Create sparse raw disk image',
             'truncate -s %dG %s' % (self.disk_gb, raw_disk), 30),

            ('Partition the disk',
             'parted -s %s mklabel gpt mkpart primary ext4 1MiB 100%%' % raw_disk, 30),

            ('Set up loop device',
             'LOOPDEV=$(losetup --find --show -P %s) && echo $LOOPDEV' % raw_disk, 30),
        ]

        # Run the initial disk steps
        loop_dev = None
        try:
            for desc, cmd, timeout in disk_steps:
                result = ssh_run(self.host, cmd, timeout=timeout)
                if result.returncode != 0:
                    return StepResult(False, '%s failed: %s' % (desc, result.stderr.strip()))
                if 'loop device' in desc.lower() or 'Set up loop' in desc:
                    loop_dev = result.stdout.strip()

            if not loop_dev:
                return StepResult(False, 'Failed to determine loop device')

            loop_part = '%sp1' % loop_dev

            # Format, mount, extract, configure
            creation_commands = [
                ('Format partition',
                 'mkfs.ext4 -q %s' % loop_part, 60),

                ('Create mount point',
                 'mkdir -p %s' % mount_point, 10),

                ('Mount partition',
                 'mount %s %s' % (loop_part, mount_point), 30),

                ('Extract stage3',
                 'tar xpf %s -C %s --xattrs-include=\'*.*\' --numeric-owner'
                 % (self.remote_tarball, mount_point), 600),

                ('Write fstab',
                 'echo "/dev/sda1 / ext4 defaults 0 1" > %s/etc/fstab' % mount_point, 10),

                ('Set hostname',
                 'echo \'hostname="%s"\' > %s/etc/conf.d/hostname' % (self.name, mount_point), 10),

                ('Configure networking',
                 self._network_config_cmd(mount_point), 10),

                ('Create net.eth0 init script',
                 'test -f %s/etc/init.d/net.lo && '
                 'ln -sf net.lo %s/etc/init.d/net.eth0 || true' % (mount_point, mount_point), 10),

                ('Enable sshd',
                 'mkdir -p %s/etc/runlevels/default && '
                 'ln -sf /etc/init.d/sshd %s/etc/runlevels/default/sshd'
                 % (mount_point, mount_point), 10),

                ('Enable networking',
                 'ln -sf /etc/init.d/net.eth0 %s/etc/runlevels/default/net.eth0'
                 % mount_point, 10),
            ]

            # SSH key injection
            if self.ssh_pubkey:
                escaped_key = self.ssh_pubkey.replace("'", "'\\''")
                creation_commands.append((
                    'Inject SSH key',
                    'mkdir -p %s/root/.ssh && chmod 700 %s/root/.ssh && '
                    'echo \'%s\' >> %s/root/.ssh/authorized_keys && '
                    'chmod 600 %s/root/.ssh/authorized_keys'
                    % (mount_point, mount_point, escaped_key, mount_point, mount_point),
                    10,
                ))

            for desc, cmd, timeout in creation_commands:
                result = ssh_run(self.host, cmd, timeout=timeout)
                if result.returncode != 0:
                    # Try to clean up mount
                    ssh_run(self.host, 'umount %s 2>/dev/null; '
                            'losetup -d %s 2>/dev/null' % (mount_point, loop_dev),
                            timeout=30)
                    return StepResult(False, '%s failed: %s' % (desc, result.stderr.strip()))

            # Unmount and detach loop device
            result = ssh_run(self.host, 'umount %s' % mount_point, timeout=30)
            if result.returncode != 0:
                return StepResult(False, 'Unmount failed: %s' % result.stderr.strip())

            result = ssh_run(self.host, 'losetup -d %s' % loop_dev, timeout=30)
            if result.returncode != 0:
                return StepResult(False, 'Loop detach failed: %s' % result.stderr.strip())

        except Exception as exc:
            # Best-effort cleanup
            if loop_dev:
                ssh_run(self.host, 'umount %s 2>/dev/null; '
                        'losetup -d %s 2>/dev/null' % (mount_point, loop_dev),
                        timeout=30)
            return StepResult(False, 'Disk creation failed: %s' % exc)

        # --- Create VM via qm ---
        try:
            qm_create_cmd = (
                'qm create %d'
                ' --name %s'
                ' --cores %d'
                ' --memory %d'
                ' --net0 virtio,bridge=%s'
                ' --ostype l26'
                ' --scsihw virtio-scsi-pci'
                ' --agent enabled=0'
                % (self.vmid, self.name, self.cores, self.ram_mb, self.bridge)
            )
            result = ssh_run(self.host, qm_create_cmd, timeout=30)
            if result.returncode != 0:
                return StepResult(False, 'qm create failed: %s' % result.stderr.strip())

            # Import disk
            import_cmd = 'qm importdisk %d %s %s' % (self.vmid, raw_disk, self.storage)
            result = ssh_run(self.host, import_cmd, timeout=600)
            if result.returncode != 0:
                return StepResult(False, 'qm importdisk failed: %s' % result.stderr.strip())

            # Attach the imported disk and set boot order
            attach_cmd = (
                'qm set %d --scsi0 %s:vm-%d-disk-0 --boot order=scsi0'
                % (self.vmid, self.storage, self.vmid)
            )
            result = ssh_run(self.host, attach_cmd, timeout=30)
            if result.returncode != 0:
                return StepResult(False, 'qm set (attach disk) failed: %s' % result.stderr.strip())

            # Clean up temporary raw disk on remote host
            ssh_run(self.host, 'rm -f %s' % raw_disk, timeout=10)

        except Exception as exc:
            return StepResult(False, 'VM creation failed: %s' % exc)

        return StepResult(True, 'VM %d (%s) created on %s' % (self.vmid, self.name, self.host))

    def _network_config_cmd(self, mount_point):
        """Return a shell command to write the network config file."""
        conf_path = '%s/etc/conf.d/net' % mount_point
        if self.ip:
            gateway = re.sub(r'\.\d+$', '.1', self.ip)
            return (
                'printf \'config_eth0="%s/24"\\nroutes_eth0="default via %s"\\n\' > %s'
                % (self.ip, gateway, conf_path)
            )
        return 'echo \'config_eth0="dhcp"\' > %s' % conf_path

    # ------------------------------------------------------------------
    # Network / SSH key (already done in create)
    # ------------------------------------------------------------------

    def configure_network(self):
        """Network was configured during disk image creation."""
        return StepResult(True, 'Network configured during disk creation')

    def inject_ssh_key(self, pubkey):
        """SSH key was injected during disk image creation."""
        if pubkey and not self.ssh_pubkey:
            self.ssh_pubkey = pubkey
        return StepResult(True, 'SSH key injected during disk creation')

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def start(self):
        """Start the VM via qm start."""
        if not self.vmid:
            return StepResult(False, 'No VMID set')

        try:
            result = ssh_run(self.host, 'qm start %d' % self.vmid, timeout=60)
            if result.returncode != 0:
                return StepResult(False, 'qm start failed: %s' % result.stderr.strip())
        except Exception as exc:
            return StepResult(False, 'Failed to start VM: %s' % exc)

        return StepResult(True, 'VM %d started on %s' % (self.vmid, self.host))

    # ------------------------------------------------------------------
    # Wait for SSH
    # ------------------------------------------------------------------

    def wait_for_ssh(self, timeout=180):
        """Wait for the VM to become reachable over SSH.

        Tries to discover the IP via qm guest cmd or ARP if not already
        known.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            if not self.ip:
                self.ip = self._discover_ip()

            if self.ip and ssh_test(self.ip):
                return StepResult(True, 'SSH reachable at %s' % self.ip)

            time.sleep(5)

        if not self.ip:
            return StepResult(False, 'Could not discover VM IP within %ds' % timeout)
        return StepResult(False, 'SSH not reachable at %s within %ds' % (self.ip, timeout))

    def _discover_ip(self):
        """Try to discover the VM's IP address from Proxmox."""
        # Method 1: QEMU guest agent (if installed in the VM)
        try:
            result = ssh_run(
                self.host,
                'qm guest cmd %d network-get-interfaces' % self.vmid,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for iface in data:
                    iface_name = iface.get('name', '')
                    if iface_name == 'lo':
                        continue
                    for addr in iface.get('ip-addresses', []):
                        if addr.get('ip-address-type') == 'ipv4':
                            ip = addr.get('ip-address')
                            if ip and not ip.startswith('127.'):
                                return ip
        except (json.JSONDecodeError, KeyError, Exception):
            pass

        # Method 2: ARP table on the Proxmox host
        # First, try to get the VM's MAC address from qm config
        mac_address = None
        try:
            result = ssh_run(
                self.host,
                'qm config %d' % self.vmid,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith('net0:'):
                        match = re.search(
                            r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', line,
                        )
                        if match:
                            mac_address = match.group(1).lower()
                            break
        except Exception:
            pass

        if mac_address:
            try:
                result = ssh_run(
                    self.host,
                    'arp -n | grep %s' % mac_address,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                    if match:
                        return match.group(1)
            except Exception:
                pass

        return None

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def get_ip(self):
        """Return the VM's IP address (may be None if not yet discovered)."""
        return self.ip

    def cleanup_on_failure(self):
        """Stop and destroy the VM on the Proxmox host."""
        if not self.vmid or not self.host:
            return StepResult(False, 'No VMID or host set for cleanup')

        try:
            result = ssh_run(
                self.host,
                'qm stop %d 2>/dev/null; qm destroy %d 2>/dev/null' % (self.vmid, self.vmid),
                timeout=60,
            )

            # Also clean up temporary files
            cleanup_files = []
            if self.remote_tarball:
                cleanup_files.append(self.remote_tarball)
            if self.remote_raw_disk:
                cleanup_files.append(self.remote_raw_disk)
            if cleanup_files:
                ssh_run(
                    self.host,
                    'rm -f %s' % ' '.join(cleanup_files),
                    timeout=10,
                )

        except Exception as exc:
            return StepResult(False, 'Cleanup failed: %s' % exc)

        return StepResult(True, 'Cleaned up VM %d on %s' % (self.vmid, self.host))

    def dry_run_summary(self):
        """Return a human-readable list of steps that would be performed."""
        host_display = self.host_alias or self.host or '<unset>'
        return [
            'Check prerequisites (SSH to %s, qm command, storage %s)' % (
                host_display, self.storage),
            'Allocate VMID in range %d-%d' % (VMID_MIN, VMID_MAX),
            'Download Gentoo stage3 tarball (or use cache)',
            'SCP stage3 tarball to %s:/tmp/' % host_display,
            'Create %dG raw disk image on remote host' % self.disk_gb,
            'Partition (GPT), format (ext4), mount via loop device',
            'Extract stage3 into disk image',
            'Configure fstab, hostname (%s), networking (%s)' % (
                self.name or '<unset>',
                'static %s' % self.ip if self.ip else 'DHCP'),
            'Enable sshd and networking at boot',
            'Inject SSH public key into root authorized_keys',
            'qm create %s: %d cores, %dMB RAM, bridge=%s, scsi, l26' % (
                self.name or '<unset>', self.cores, self.ram_mb, self.bridge),
            'qm importdisk to %s storage' % self.storage,
            'Attach disk as scsi0, set boot order',
            'qm start %s' % (str(self.vmid) if self.vmid else '<unset>'),
            'Wait for SSH (up to 180s), discover IP via guest agent or ARP',
        ]
