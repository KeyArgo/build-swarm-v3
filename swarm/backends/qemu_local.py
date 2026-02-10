"""
Standalone QEMU/KVM backend -- creates VMs locally using virsh/virt-install
or qemu-system-x86_64.

Part of the build-swarm-v3 project.
"""

import os
import re
import shutil
import subprocess
import time

from swarm.backends import DroneBackend, StepResult, register_backend, ssh_test


DISK_DIR = '/var/lib/libvirt/images'
MOUNT_POINT = '/mnt/gentoo-stage3'


@register_backend('qemu')
class QEMULocalBackend(DroneBackend):
    """Standalone QEMU/KVM VM (virsh/virt-install)."""

    DESCRIPTION = 'Standalone QEMU/KVM VM (virsh/virt-install)'

    def __init__(self, name=None, ip=None, cores=4, ram_mb=4096, disk_gb=50,
                 bridge='virbr0', ssh_pubkey=None, **kwargs):
        self.name = name
        self.ip = ip
        self.cores = cores
        self.ram_mb = ram_mb
        self.disk_gb = disk_gb
        self.bridge = bridge
        self.ssh_pubkey = ssh_pubkey
        self.disk_path = None
        self.use_virsh = True  # prefer virsh over raw qemu
        self.tarball_path = None
        self.kernel_path = None
        self.initrd_path = None

    # ------------------------------------------------------------------
    # Availability / prerequisites
    # ------------------------------------------------------------------

    @classmethod
    def probe_availability(cls):
        """Check for virsh OR qemu-system-x86_64 and /dev/kvm."""
        has_virsh = shutil.which('virsh') is not None
        has_qemu = shutil.which('qemu-system-x86_64') is not None
        has_kvm = os.path.exists('/dev/kvm')

        if (has_virsh or has_qemu) and has_kvm:
            return 'available'
        return 'unavailable'

    def check_prerequisites(self):
        """Verify all tools and paths needed to create a QEMU VM."""
        errors = []

        has_virsh = shutil.which('virsh') is not None
        has_qemu = shutil.which('qemu-system-x86_64') is not None

        if not has_virsh and not has_qemu:
            errors.append('Neither virsh nor qemu-system-x86_64 found in PATH')

        if has_virsh:
            self.use_virsh = True
        elif has_qemu:
            self.use_virsh = False

        if not os.path.exists('/dev/kvm'):
            errors.append('/dev/kvm not found -- KVM acceleration unavailable')

        if not os.path.isdir(DISK_DIR):
            errors.append('Disk directory %s does not exist' % DISK_DIR)
        elif not os.access(DISK_DIR, os.W_OK):
            errors.append('Disk directory %s is not writable' % DISK_DIR)

        if shutil.which('qemu-img') is None:
            errors.append('qemu-img not found (needed for disk creation)')

        if shutil.which('qemu-nbd') is None:
            errors.append('qemu-nbd not found (needed for disk setup)')

        if errors:
            return StepResult(False, '; '.join(errors))
        return StepResult(True, 'All prerequisites satisfied')

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------

    def allocate_id(self):
        """Ensure the VM name is not already in use and set disk path."""
        if not self.name:
            return StepResult(False, 'No VM name specified')

        if self.use_virsh and shutil.which('virsh'):
            try:
                result = subprocess.run(
                    ['virsh', 'list', '--all', '--name'],
                    capture_output=True, text=True, timeout=10,
                )
                existing = [n.strip() for n in result.stdout.splitlines() if n.strip()]
                if self.name in existing:
                    return StepResult(False, 'VM name %r already in use' % self.name)
            except subprocess.TimeoutExpired:
                return StepResult(False, 'virsh list timed out')
            except Exception as exc:
                return StepResult(False, 'Failed to query virsh: %s' % exc)

        self.disk_path = os.path.join(DISK_DIR, '%s.qcow2' % self.name)

        if os.path.exists(self.disk_path):
            return StepResult(False, 'Disk image already exists: %s' % self.disk_path)

        return StepResult(True, 'Allocated VM name=%s disk=%s' % (self.name, self.disk_path))

    # ------------------------------------------------------------------
    # Image download
    # ------------------------------------------------------------------

    def download_image(self, cache_dir):
        """Download or locate a cached Gentoo stage3 tarball."""
        from swarm.backends.stage3 import download_stage3, find_cached_stage3

        cached = find_cached_stage3(cache_dir)
        if cached:
            self.tarball_path = cached
            return StepResult(True, 'Using cached stage3: %s' % cached)

        tarball = download_stage3(cache_dir)
        if tarball and os.path.isfile(tarball):
            self.tarball_path = tarball
            return StepResult(True, 'Downloaded stage3: %s' % tarball)

        return StepResult(False, 'Failed to download stage3 tarball')

    # ------------------------------------------------------------------
    # Disk creation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run(cmd, timeout=120, check=True):
        """Run a command, returning (returncode, stdout, stderr)."""
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                'Command %r failed (rc=%d): %s' % (
                    ' '.join(cmd), result.returncode,
                    result.stderr.strip() or result.stdout.strip(),
                )
            )
        return result.returncode, result.stdout, result.stderr

    def _extract_kernel_initrd(self, root):
        """Find a kernel and initramfs inside the extracted stage3 tree."""
        boot_dir = os.path.join(root, 'boot')
        kernel = None
        initrd = None

        if os.path.isdir(boot_dir):
            for entry in sorted(os.listdir(boot_dir), reverse=True):
                path = os.path.join(boot_dir, entry)
                if not os.path.isfile(path):
                    continue
                lower = entry.lower()
                if kernel is None and (lower.startswith('vmlinuz') or lower.startswith('kernel')):
                    kernel = path
                if initrd is None and (lower.startswith('initramfs') or lower.startswith('initrd')):
                    initrd = path

        return kernel, initrd

    def _write_file(self, path, content, mode=0o644):
        """Write *content* to *path*, creating parent dirs as needed."""
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent, mode=0o755, exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(content)
        os.chmod(path, mode)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self):
        """Build a bootable QEMU disk image from the stage3 tarball.

        Uses qemu-nbd to mount a qcow2 image, extracts the stage3 into it,
        writes basic configuration, then disconnects.  The VM will be booted
        with direct kernel boot (no grub required).
        """
        if not self.disk_path:
            return StepResult(False, 'disk_path not set -- call allocate_id() first')
        if not self.tarball_path:
            return StepResult(False, 'tarball_path not set -- call download_image() first')

        nbd_device = '/dev/nbd0'
        nbd_part = '/dev/nbd0p1'

        try:
            # 1. Create qcow2 disk image
            self._run(['qemu-img', 'create', '-f', 'qcow2',
                        self.disk_path, '%dG' % self.disk_gb])

            # 2. Load nbd module
            self._run(['modprobe', 'nbd', 'max_part=8'], check=False)

            # 3. Connect disk to nbd
            self._run(['qemu-nbd', '-c', nbd_device, self.disk_path])

            # Give the kernel a moment to register partition devices
            time.sleep(1)

            # 4. Partition the disk
            self._run([
                'parted', '-s', nbd_device,
                'mklabel', 'gpt',
                'mkpart', 'primary', 'ext4', '1MiB', '100%',
            ])

            # Wait for partition device to appear
            for _ in range(10):
                if os.path.exists(nbd_part):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('Partition device %s did not appear' % nbd_part)

            # 5. Format
            self._run(['mkfs.ext4', '-q', nbd_part])

            # 6. Mount
            os.makedirs(MOUNT_POINT, exist_ok=True)
            self._run(['mount', nbd_part, MOUNT_POINT])

            # 7. Extract stage3
            self._run([
                'tar', 'xpf', self.tarball_path,
                '-C', MOUNT_POINT,
                '--xattrs-include=*.*',
                '--numeric-owner',
            ], timeout=600)

            # 8. Configure basics ----------------------------------------

            # fstab
            self._write_file(
                os.path.join(MOUNT_POINT, 'etc/fstab'),
                '# /etc/fstab - generated by build-swarm qemu backend\n'
                '/dev/sda1\t/\text4\tdefaults\t0 1\n',
            )

            # hostname
            self._write_file(
                os.path.join(MOUNT_POINT, 'etc/conf.d/hostname'),
                'hostname="%s"\n' % self.name,
            )

            # networking
            if self.ip:
                net_content = (
                    '# Static IP configuration\n'
                    'config_eth0="%s/24"\n'
                    'routes_eth0="default via %s"\n'
                    % (self.ip, re.sub(r'\.\d+$', '.1', self.ip))
                )
            else:
                net_content = (
                    '# DHCP configuration\n'
                    'config_eth0="dhcp"\n'
                )
            self._write_file(
                os.path.join(MOUNT_POINT, 'etc/conf.d/net'),
                net_content,
            )

            # Create net.eth0 init script symlink (from net.lo)
            net_lo = os.path.join(MOUNT_POINT, 'etc/init.d/net.lo')
            net_eth0 = os.path.join(MOUNT_POINT, 'etc/init.d/net.eth0')
            if os.path.exists(net_lo) and not os.path.exists(net_eth0):
                os.symlink('net.lo', net_eth0)

            # Enable sshd at boot
            default_runlevel = os.path.join(MOUNT_POINT, 'etc/runlevels/default')
            os.makedirs(default_runlevel, exist_ok=True)

            sshd_link = os.path.join(default_runlevel, 'sshd')
            if not os.path.exists(sshd_link):
                os.symlink('/etc/init.d/sshd', sshd_link)

            # Enable networking at boot
            net_link = os.path.join(default_runlevel, 'net.eth0')
            if not os.path.exists(net_link):
                os.symlink('/etc/init.d/net.eth0', net_link)

            # 9. Inject SSH pubkey
            if self.ssh_pubkey:
                ssh_dir = os.path.join(MOUNT_POINT, 'root/.ssh')
                os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
                auth_keys = os.path.join(ssh_dir, 'authorized_keys')
                with open(auth_keys, 'a') as fh:
                    fh.write(self.ssh_pubkey.rstrip('\n') + '\n')
                os.chmod(auth_keys, 0o600)

            # 10. Extract kernel / initramfs for direct kernel boot
            kernel, initrd = self._extract_kernel_initrd(MOUNT_POINT)
            if kernel:
                # Copy kernel and initrd out of the mounted image so we can
                # reference them for direct kernel boot after unmount.
                dst_dir = os.path.join(DISK_DIR, '%s-boot' % self.name)
                os.makedirs(dst_dir, exist_ok=True)

                import shutil as _shutil
                self.kernel_path = os.path.join(dst_dir, os.path.basename(kernel))
                _shutil.copy2(kernel, self.kernel_path)

                if initrd:
                    self.initrd_path = os.path.join(dst_dir, os.path.basename(initrd))
                    _shutil.copy2(initrd, self.initrd_path)

            # 11. Unmount
            self._run(['umount', MOUNT_POINT])

            # 12. Disconnect nbd
            self._run(['qemu-nbd', '-d', nbd_device])

        except Exception as exc:
            # Best-effort cleanup on failure
            subprocess.run(['umount', MOUNT_POINT], capture_output=True)
            subprocess.run(['qemu-nbd', '-d', nbd_device], capture_output=True)
            return StepResult(False, 'Disk creation failed: %s' % exc)

        return StepResult(True, 'Disk image created: %s' % self.disk_path)

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
        """Start the VM via virsh/virt-install or raw qemu."""
        if not self.name or not self.disk_path:
            return StepResult(False, 'VM not fully configured (name or disk missing)')

        if self.use_virsh and shutil.which('virt-install'):
            try:
                virt_cmd = [
                    'virt-install',
                    '--name', self.name,
                    '--ram', str(self.ram_mb),
                    '--vcpus', str(self.cores),
                    '--disk', 'path=%s,format=qcow2' % self.disk_path,
                    '--network', 'bridge=%s,model=virtio' % self.bridge,
                    '--os-variant', 'gentoo',
                    '--import',
                    '--noautoconsole',
                    '--noreboot',
                ]

                # Direct kernel boot if we extracted a kernel
                if self.kernel_path:
                    virt_cmd += ['--boot', 'kernel=%s,kernel_args=root=/dev/sda1 console=ttyS0' % self.kernel_path]
                    if self.initrd_path:
                        virt_cmd[-1] += ',initrd=%s' % self.initrd_path

                self._run(virt_cmd, timeout=60)
                self._run(['virsh', 'start', self.name], timeout=30)

            except Exception as exc:
                return StepResult(False, 'virt-install/virsh start failed: %s' % exc)
        else:
            return StepResult(False, 'Raw qemu-system-x86_64 launch not yet implemented')

        return StepResult(True, 'VM %s started' % self.name)

    # ------------------------------------------------------------------
    # Wait for SSH
    # ------------------------------------------------------------------

    def wait_for_ssh(self, timeout=180):
        """Wait for the VM to become reachable over SSH.

        QEMU VMs typically take longer to boot than containers, so the
        default timeout is 180 seconds.  If DHCP is used we try to discover
        the IP via ``virsh domifaddr`` or ARP.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            # Try to discover IP if we don't have one yet
            if not self.ip:
                self.ip = self._discover_ip()

            if self.ip:
                if ssh_test(self.ip):
                    return StepResult(True, 'SSH reachable at %s' % self.ip)

            time.sleep(5)

        if not self.ip:
            return StepResult(False, 'Could not discover VM IP within %ds' % timeout)
        return StepResult(False, 'SSH not reachable at %s within %ds' % (self.ip, timeout))

    def _discover_ip(self):
        """Try to discover the IP of the running VM."""
        # Method 1: virsh domifaddr
        if shutil.which('virsh'):
            try:
                rc, stdout, _ = self._run(
                    ['virsh', 'domifaddr', self.name],
                    timeout=10, check=False,
                )
                if rc == 0 and stdout.strip():
                    # Parse output like:
                    #  Name       MAC address          Protocol     Address
                    #  vnet0      52:54:00:xx:xx:xx    ipv4         192.168.x.x/24
                    for line in stdout.splitlines():
                        match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                        if match:
                            return match.group(1)
            except Exception:
                pass

        # Method 2: ARP table scan
        try:
            rc, stdout, _ = self._run(['arp', '-n'], timeout=10, check=False)
            if rc == 0:
                for line in stdout.splitlines():
                    # ARP entries for the bridge network
                    match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                    if match:
                        candidate = match.group(1)
                        # Quick check -- does this respond to SSH?
                        if ssh_test(candidate, timeout=2):
                            return candidate
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
        """Destroy and undefine the VM, removing its storage."""
        if not self.name:
            return StepResult(False, 'No VM name to clean up')

        errors = []

        if shutil.which('virsh'):
            rc, _, stderr = self._run(
                ['virsh', 'destroy', self.name], timeout=30, check=False,
            )
            if rc != 0 and 'domain is not running' not in stderr:
                errors.append('virsh destroy: %s' % stderr.strip())

            rc, _, stderr = self._run(
                ['virsh', 'undefine', self.name, '--remove-all-storage'],
                timeout=30, check=False,
            )
            if rc != 0 and 'failed to get domain' not in stderr.lower():
                errors.append('virsh undefine: %s' % stderr.strip())
        else:
            # Manual cleanup -- remove the disk image
            if self.disk_path and os.path.exists(self.disk_path):
                try:
                    os.remove(self.disk_path)
                except OSError as exc:
                    errors.append('Could not remove disk: %s' % exc)

        # Remove extracted boot files
        boot_dir = os.path.join(DISK_DIR, '%s-boot' % self.name)
        if os.path.isdir(boot_dir):
            import shutil as _shutil
            _shutil.rmtree(boot_dir, ignore_errors=True)

        if errors:
            return StepResult(False, '; '.join(errors))
        return StepResult(True, 'Cleaned up VM %s' % self.name)

    def dry_run_summary(self):
        """Return a human-readable list of steps that *would* be performed."""
        return [
            'Check prerequisites (virsh/qemu, /dev/kvm, disk dir, qemu-img, qemu-nbd)',
            'Allocate VM name=%s, disk at %s/%s.qcow2' % (
                self.name or '<unset>', DISK_DIR, self.name or '<unset>'),
            'Download Gentoo stage3 tarball (or use cache)',
            'Create %dG qcow2 disk image' % self.disk_gb,
            'Connect disk via qemu-nbd, partition (GPT), format (ext4)',
            'Extract stage3 into disk image',
            'Configure /etc/fstab, hostname (%s), networking (%s)' % (
                self.name or '<unset>',
                'static %s' % self.ip if self.ip else 'DHCP'),
            'Enable sshd and networking at boot',
            'Inject SSH public key into root authorized_keys',
            'Extract kernel/initramfs for direct kernel boot',
            'Unmount and disconnect nbd',
            'Define VM via virt-install: %d cores, %dMB RAM, bridge=%s' % (
                self.cores, self.ram_mb, self.bridge),
            'Start VM via virsh',
            'Wait for SSH (up to 180s)',
        ]
