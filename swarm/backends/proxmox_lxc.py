"""
Proxmox LXC backend -- creates Gentoo LXC containers on Proxmox VE hosts
via SSH + pct commands.

This backend connects to a Proxmox VE hypervisor over SSH and uses the
pct CLI to create, configure, start, and destroy unprivileged LXC
containers that serve as build drones.
"""

import os
import re
import subprocess
import time

from swarm.backends import DroneBackend, StepResult, register_backend, ssh_run, ssh_test

# ---------------------------------------------------------------------------
# Known Proxmox hosts in the local network
# ---------------------------------------------------------------------------
KNOWN_PROXMOX_HOSTS = {
    "proxmox-io":    "10.0.0.2",
    "proxmox-titan": "10.0.0.3",
}


@register_backend("proxmox-lxc")
class ProxmoxLXCBackend(DroneBackend):
    """Proxmox LXC container (via SSH + pct)."""

    DESCRIPTION = "Proxmox LXC container (via SSH + pct)"

    # -- construction -------------------------------------------------------

    def __init__(
        self,
        host=None,
        name=None,
        vmid=None,
        ip=None,
        cores=4,
        ram_mb=4096,
        disk_gb=50,
        storage="local-lvm",
        bridge="vmbr0",
        ssh_pubkey=None,
        **kwargs,
    ):
        self.host = host
        self.name = name
        self.vmid = vmid
        self.ip = ip
        self.cores = cores
        self.ram_mb = ram_mb
        self.disk_gb = disk_gb
        self.storage = storage
        self.bridge = bridge
        self.ssh_pubkey = ssh_pubkey
        self.template_path = None  # populated by download_image()

    # -- availability -------------------------------------------------------

    @classmethod
    def probe_availability(cls):
        """Return 'available' if any known Proxmox host responds to SSH."""
        for _label, addr in KNOWN_PROXMOX_HOSTS.items():
            if ssh_test(addr):
                return "available"
        return "unavailable"

    # -- prerequisites ------------------------------------------------------

    def check_prerequisites(self):
        """Verify that the target Proxmox host is reachable and usable."""
        if not self.host:
            return StepResult.fail(
                "Proxmox host required. "
                "Use --host 10.0.0.2 or --host 10.0.0.3"
            )

        if not ssh_test(self.host):
            return StepResult.fail(
                "Cannot reach Proxmox host %s via SSH" % self.host
            )

        # Verify pct is available
        result = ssh_run(self.host, "which pct")
        if result.returncode != 0:
            return StepResult.fail(
                "pct command not found on %s -- is this a Proxmox VE node?"
                % self.host
            )

        # Verify the requested storage pool exists
        result = ssh_run(self.host, "pvesm status")
        if result.returncode != 0:
            return StepResult.fail(
                "Failed to query storage on %s: %s"
                % (self.host, result.stderr.strip())
            )

        storage_names = []
        for line in result.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if parts:
                storage_names.append(parts[0])

        if self.storage not in storage_names:
            return StepResult.fail(
                "Storage '%s' not found on %s. Available: %s"
                % (self.storage, self.host, ", ".join(storage_names))
            )

        return StepResult.success(
            "Proxmox host %s is reachable; pct present; storage '%s' OK"
            % (self.host, self.storage)
        )

    # -- VMID allocation ----------------------------------------------------

    def allocate_id(self):
        """Pick or verify a VMID in the 200-299 range."""
        if self.vmid is not None:
            # Verify the requested VMID is not already taken
            result = ssh_run(self.host, "pct status %s" % self.vmid)
            if result.returncode == 0:
                return StepResult.fail(
                    "VMID %s is already in use on %s"
                    % (self.vmid, self.host)
                )
            return StepResult.success("VMID %s is free" % self.vmid)

        # Collect all VMIDs currently in use (both LXC and QEMU)
        result = ssh_run(
            self.host,
            "cat <(pct list) <(qm list) 2>/dev/null",
        )
        used_ids = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                try:
                    used_ids.add(int(parts[0]))
                except ValueError:
                    continue

        # Find next unused VMID in 200-299
        for candidate in range(200, 300):
            if candidate not in used_ids:
                self.vmid = candidate
                return StepResult.success(
                    "Allocated VMID %s on %s" % (self.vmid, self.host)
                )

        return StepResult.fail(
            "No free VMID in range 200-299 on %s" % self.host
        )

    # -- image / template ---------------------------------------------------

    def download_image(self, cache_dir):
        """Ensure a Gentoo stage3 template is present on the Proxmox host."""
        # Check whether a cached template already exists
        result = ssh_run(
            self.host,
            "ls /var/lib/vz/template/cache/"
            "gentoo-stage3-amd64-openrc*.tar.xz 2>/dev/null",
        )
        if result.returncode == 0 and result.stdout.strip():
            # Use the first matching template
            full_path = result.stdout.strip().splitlines()[0]
            self.template_path = os.path.basename(full_path)
            return StepResult.success(
                "Gentoo template already cached: %s" % self.template_path
            )

        # Not cached -- try to download via pveam
        result = ssh_run(
            self.host,
            "pveam available --section system | grep gentoo",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return StepResult.fail(
                "No Gentoo template available via pveam on %s. "
                "Please manually download a Gentoo stage3 openrc template "
                "into /var/lib/vz/template/cache/ on the Proxmox host."
                % self.host
            )

        # Parse template name (second column of pveam available output)
        template_name = None
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                template_name = parts[1]
                break

        if not template_name:
            return StepResult.fail(
                "Could not parse template name from pveam output on %s"
                % self.host
            )

        # Download the template (may take a while)
        result = ssh_run(
            self.host,
            "pveam download local %s" % template_name,
            timeout=600,
        )
        if result.returncode != 0:
            return StepResult.fail(
                "pveam download failed on %s: %s"
                % (self.host, result.stderr.strip() or result.stdout.strip())
            )

        self.template_path = template_name
        return StepResult.success(
            "Downloaded Gentoo template: %s" % self.template_path
        )

    # -- create container ---------------------------------------------------

    def create(self):
        """Create the LXC container via pct create."""
        if self.template_path is None:
            return StepResult.fail(
                "No template available -- run download_image() first"
            )
        if self.vmid is None:
            return StepResult.fail(
                "No VMID allocated -- run allocate_id() first"
            )

        hostname = self.name or ("build-drone-%s" % self.vmid)

        # Build the network option
        if self.ip:
            net0 = (
                "name=eth0,bridge=%s,ip=%s/24,gw=10.0.0.1"
                % (self.bridge, self.ip)
            )
        else:
            net0 = "name=eth0,bridge=%s,ip=dhcp" % self.bridge

        cmd = (
            "pct create {vmid} local:vztmpl/{template}"
            " --hostname {hostname}"
            " --cores {cores}"
            " --memory {ram_mb}"
            " --rootfs {storage}:{disk_gb}"
            " --net0 {net0}"
            " --ostype gentoo"
            " --unprivileged 0"
            " --features nesting=1"
            " --start 0"
        ).format(
            vmid=self.vmid,
            template=self.template_path,
            hostname=hostname,
            cores=self.cores,
            ram_mb=self.ram_mb,
            storage=self.storage,
            disk_gb=self.disk_gb,
            net0=net0,
        )

        result = ssh_run(self.host, cmd, timeout=120)
        if result.returncode != 0:
            return StepResult.fail(
                "pct create failed (VMID %s): %s"
                % (self.vmid, result.stderr.strip() or result.stdout.strip())
            )

        return StepResult.success(
            "Created LXC container %s (VMID %s) on %s"
            % (hostname, self.vmid, self.host)
        )

    # -- network configuration ----------------------------------------------

    def configure_network(self):
        """Verify network config (already applied during create())."""
        result = ssh_run(
            self.host,
            "pct config %s | grep net0" % self.vmid,
        )
        if result.returncode != 0 or "net0" not in result.stdout:
            return StepResult.fail(
                "net0 not found in config for VMID %s" % self.vmid
            )
        return StepResult.success(
            "Network configured via pct create: %s" % result.stdout.strip()
        )

    # -- SSH key injection --------------------------------------------------

    def inject_ssh_key(self, pubkey):
        """Write an authorized_keys file into the container rootfs."""
        if not pubkey:
            pubkey = self.ssh_pubkey
        if not pubkey:
            return StepResult.fail("No SSH public key provided")

        # The pubkey may contain spaces, +, /, @, = etc.  We single-quote
        # the whole echo argument on the remote side so the shell does not
        # interpret any of those characters.  To safely embed into a
        # single-quoted string we replace every ' with '"'"'.
        safe_key = pubkey.replace("'", "'\"'\"'")

        rootfs = "/var/lib/lxc/%s/rootfs" % self.vmid
        cmd = (
            "mkdir -p {rootfs}/root/.ssh && "
            "echo '{key}' > {rootfs}/root/.ssh/authorized_keys && "
            "chmod 700 {rootfs}/root/.ssh && "
            "chmod 600 {rootfs}/root/.ssh/authorized_keys"
        ).format(rootfs=rootfs, key=safe_key)

        result = ssh_run(self.host, cmd)
        if result.returncode != 0:
            return StepResult.fail(
                "Failed to inject SSH key into VMID %s: %s"
                % (self.vmid, result.stderr.strip() or result.stdout.strip())
            )

        return StepResult.success(
            "SSH public key injected into VMID %s" % self.vmid
        )

    # -- start --------------------------------------------------------------

    def start(self):
        """Start the LXC container and wait briefly for boot."""
        result = ssh_run(self.host, "pct start %s" % self.vmid)
        if result.returncode != 0:
            return StepResult.fail(
                "pct start failed for VMID %s: %s"
                % (self.vmid, result.stderr.strip() or result.stdout.strip())
            )
        time.sleep(3)
        return StepResult.success(
            "Started LXC container VMID %s" % self.vmid
        )

    # -- wait for SSH -------------------------------------------------------

    def wait_for_ssh(self, timeout=120):
        """Poll until SSH is reachable inside the container."""
        deadline = time.time() + timeout
        target_ip = self.ip  # may be None if DHCP

        while time.time() < deadline:
            # If we do not yet know the IP, try to discover it from the
            # container's network stack.
            if target_ip is None:
                result = ssh_run(
                    self.host,
                    "pct exec %s -- ip -4 addr show eth0" % self.vmid,
                )
                if result.returncode == 0:
                    match = re.search(
                        r"inet\s+(\d+\.\d+\.\d+\.\d+)/", result.stdout
                    )
                    if match:
                        target_ip = match.group(1)

            if target_ip is not None and ssh_test(target_ip):
                self.ip = target_ip
                return StepResult.success(
                    "SSH reachable on %s (VMID %s)" % (self.ip, self.vmid)
                )

            time.sleep(5)

        if target_ip is None:
            return StepResult.fail(
                "Could not discover IP for VMID %s within %ds"
                % (self.vmid, timeout)
            )
        return StepResult.fail(
            "SSH not reachable on %s (VMID %s) within %ds"
            % (target_ip, self.vmid, timeout)
        )

    # -- getters ------------------------------------------------------------

    def get_ip(self):
        """Return the container's IP address (may be None before start)."""
        return self.ip

    # -- cleanup ------------------------------------------------------------

    def cleanup_on_failure(self):
        """Stop and destroy the container, ignoring errors."""
        ssh_run(
            self.host,
            "pct stop %s 2>/dev/null; pct destroy %s 2>/dev/null"
            % (self.vmid, self.vmid),
        )
        return StepResult.success("Cleaned up VMID %s" % self.vmid)

    # -- dry-run summary ----------------------------------------------------

    def dry_run_summary(self):
        """Return a human-readable list of steps this backend would take."""
        hostname = self.name or ("build-drone-%s" % (self.vmid or "???"))
        net_mode = "static IP %s" % self.ip if self.ip else "DHCP"
        template_desc = self.template_path or "gentoo-stage3-amd64-openrc-*.tar.xz"

        return [
            "Check prerequisites on Proxmox host %s" % (self.host or "<unset>"),
            "Allocate VMID in 200-299 range (or use %s)" % (self.vmid or "auto"),
            "Ensure Gentoo template is cached (%s)" % template_desc,
            (
                "pct create {vmid} -- hostname={hostname}, cores={cores}, "
                "ram={ram_mb}MB, disk={disk_gb}GB on {storage}, net={net}"
            ).format(
                vmid=self.vmid or "???",
                hostname=hostname,
                cores=self.cores,
                ram_mb=self.ram_mb,
                disk_gb=self.disk_gb,
                storage=self.storage,
                net=net_mode,
            ),
            "Inject SSH public key into container rootfs",
            "pct start %s" % (self.vmid or "???"),
            "Wait for SSH on %s" % (self.ip or "DHCP address"),
        ]
