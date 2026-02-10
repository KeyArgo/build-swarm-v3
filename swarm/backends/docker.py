"""
Docker backend for creating Gentoo build drone containers.

Manages the full lifecycle of Docker containers used as build drones:
creation, networking, SSH injection, and cleanup.
"""

import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional

from swarm.backends import DroneBackend, StepResult, register_backend

# Image priority order for Gentoo drone containers
IMAGE_PRIORITY = [
    "gentoo-drone:golden-20260110",
    "gentoo-drone:latest",
    "gentoo/stage3:latest",
]


@register_backend("docker")
class DockerBackend(DroneBackend):
    """Docker container backend for local Gentoo build drones."""

    DESCRIPTION = "Docker container (local)"

    def __init__(
        self,
        name=None,       # type: Optional[str]
        ip=None,          # type: Optional[str]
        cores=4,          # type: int
        ram_mb=4096,      # type: int
        image=None,       # type: Optional[str]
        ssh_pubkey=None,  # type: Optional[str]
        **kwargs          # type: dict
    ):
        # type: (...) -> None
        self.name = name
        self.ip = ip
        self.cores = cores
        self.ram_mb = ram_mb
        self.image = image  # None means auto-detect in download_image
        self.container_id = None  # type: Optional[str]
        self.ssh_pubkey = ssh_pubkey
        self._pubkey = ssh_pubkey  # type: Optional[str]

    @classmethod
    def probe_availability(cls):
        # type: () -> str
        """Check whether Docker is available on this system.

        Returns:
            'available' if docker info succeeds, 'unavailable' otherwise.
        """
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return "available"
            return "unavailable"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unavailable"

    def check_prerequisites(self):
        # type: () -> StepResult
        """Verify that docker is installed and the daemon is running.

        Returns:
            StepResult indicating success or describing what is missing.
        """
        if shutil.which("docker") is None:
            return StepResult.fail(
                "Docker CLI not found. Install Docker to use this backend."
            )

        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail(
                "docker info timed out. The Docker daemon may be unresponsive."
            )
        except FileNotFoundError:
            return StepResult.fail(
                "Docker CLI not found in PATH."
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            return StepResult.fail(
                "Docker daemon is not running: {}".format(stderr)
            )

        return StepResult.success("Docker is installed and daemon is running")

    def allocate_id(self):
        # type: () -> StepResult
        """Verify the container name is not already in use.

        For Docker the container name IS the drone ID.

        Returns:
            StepResult indicating whether the name is available.
        """
        if not self.name:
            return StepResult.fail("No container name specified")

        try:
            result = subprocess.run(
                [
                    "docker", "ps", "-a",
                    "--filter", "name=^{}$".format(self.name),
                    "--format", "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail("docker ps timed out")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        existing = result.stdout.strip()
        if existing:
            return StepResult.fail(
                "Container name '{}' is already in use".format(self.name)
            )

        return StepResult.success(
            "Container name '{}' is available".format(self.name)
        )

    def download_image(self, cache_dir=None):
        # type: (Optional[str]) -> StepResult
        """Ensure a suitable Docker image is available.

        Checks for images in priority order:
          1. gentoo-drone:golden-20260110
          2. gentoo-drone:latest
          3. gentoo/stage3:latest

        If none are found locally, pulls gentoo/stage3:latest.

        Args:
            cache_dir: Unused for Docker (images managed by Docker daemon).

        Returns:
            StepResult with the selected image name.
        """
        # List locally available images
        try:
            result = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail("docker images timed out")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        if result.returncode != 0:
            return StepResult.fail(
                "Failed to list Docker images: {}".format(result.stderr.strip())
            )

        available_images = set(result.stdout.strip().splitlines())

        # Check priority order
        for candidate in IMAGE_PRIORITY:
            if candidate in available_images:
                self.image = candidate
                return StepResult.success(
                    "Using existing image: {}".format(candidate)
                )

        # None found locally — pull the fallback image
        pull_target = "gentoo/stage3:latest"
        try:
            result = subprocess.run(
                ["docker", "pull", pull_target],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail(
                "docker pull {} timed out after 600 seconds".format(pull_target)
            )
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        if result.returncode != 0:
            return StepResult.fail(
                "Failed to pull {}: {}".format(pull_target, result.stderr.strip())
            )

        self.image = pull_target
        return StepResult.success("Pulled image: {}".format(pull_target))

    def create(self):
        # type: () -> StepResult
        """Create the Docker container.

        Runs docker create with the configured name, resource limits,
        and volume mounts.

        Returns:
            StepResult with the container ID.
        """
        if not self.image:
            return StepResult.fail(
                "No image set. Call download_image() first."
            )

        # Use sleep infinity as PID 1 — works with all images including
        # the golden image which has a custom entrypoint that may exit.
        cmd = [
            "docker", "create",
            "--name", self.name,
            "--hostname", self.name,
            "--privileged",
            "--cpus", str(self.cores),
            "--memory", "{}m".format(self.ram_mb),
            "--tmpfs", "/tmp",
            "-v", "/var/cache/distfiles:/var/cache/distfiles",
            "--entrypoint", "/bin/bash",
            self.image,
            "-c", "sleep infinity",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail("docker create timed out")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        if result.returncode != 0:
            return StepResult.fail(
                "docker create failed: {}".format(result.stderr.strip())
            )

        self.container_id = result.stdout.strip()
        return StepResult.success(
            "Created container {} ({})".format(self.name, self.container_id[:12])
        )

    def configure_network(self):
        # type: () -> StepResult
        """Configure networking for the container.

        Docker handles networking via its bridge driver, so no
        additional configuration is needed.

        Returns:
            StepResult indicating success.
        """
        return StepResult.success("Docker bridge network")

    def inject_ssh_key(self, pubkey):
        # type: (str) -> StepResult
        """Store the SSH public key to be injected after container start.

        The key cannot be injected now because docker exec requires a
        running container. The key will be written during start().

        Args:
            pubkey: SSH public key string.

        Returns:
            StepResult indicating the key is stored for later injection.
        """
        self._pubkey = pubkey
        return StepResult.success("SSH key will be injected after start")

    def _run_exec(self, cmd_args, timeout=60):
        # type: (List[str], int) -> subprocess.CompletedProcess
        """Run a docker exec command against this container.

        Args:
            cmd_args: Arguments to pass after 'docker exec {name}'.
            timeout: Timeout in seconds.

        Returns:
            CompletedProcess result.
        """
        full_cmd = ["docker", "exec", self.name] + cmd_args
        return subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def start(self):
        # type: () -> StepResult
        """Start the container and configure SSH access.

        Steps:
          1. docker start
          2. Wait 2 seconds for init
          3. Inject SSH public key
          4. Install sshd if missing
          5. Generate host keys and start sshd

        Returns:
            StepResult indicating whether the container started successfully.
        """
        # Start the container
        try:
            result = subprocess.run(
                ["docker", "start", self.name],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail("docker start timed out")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        if result.returncode != 0:
            return StepResult.fail(
                "docker start failed: {}".format(result.stderr.strip())
            )

        # Wait for init to come up
        time.sleep(2)

        # Inject SSH key if we have one
        if self._pubkey:
            try:
                self._run_exec(["mkdir", "-p", "/root/.ssh"], timeout=15)

                inject_cmd = (
                    "echo \"{pubkey}\" > /root/.ssh/authorized_keys "
                    "&& chmod 700 /root/.ssh "
                    "&& chmod 600 /root/.ssh/authorized_keys"
                ).format(pubkey=self._pubkey)

                result = self._run_exec(
                    ["bash", "-c", inject_cmd], timeout=15
                )
                if result.returncode != 0:
                    return StepResult.fail(
                        "Failed to inject SSH key: {}".format(
                            result.stderr.strip()
                        )
                    )
            except subprocess.TimeoutExpired:
                return StepResult.fail(
                    "Timed out injecting SSH key into container"
                )
            except FileNotFoundError:
                return StepResult.fail("Docker CLI not found")

        # Check if sshd exists
        try:
            which_result = self._run_exec(["which", "sshd"], timeout=15)
        except subprocess.TimeoutExpired:
            return StepResult.fail("Timed out checking for sshd")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        if which_result.returncode != 0:
            # Install openssh
            try:
                install_result = self._run_exec(
                    ["emerge", "--quiet", "--getbinpkg", "net-misc/openssh"],
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                return StepResult.fail(
                    "Timed out installing openssh (300s limit)"
                )
            except FileNotFoundError:
                return StepResult.fail("Docker CLI not found")

            if install_result.returncode != 0:
                return StepResult.fail(
                    "Failed to install openssh: {}".format(
                        install_result.stderr.strip()[:500]
                    )
                )

        # Generate host keys
        try:
            keygen_result = self._run_exec(
                ["ssh-keygen", "-A"], timeout=30
            )
            if keygen_result.returncode != 0:
                return StepResult.fail(
                    "ssh-keygen -A failed: {}".format(
                        keygen_result.stderr.strip()
                    )
                )
        except subprocess.TimeoutExpired:
            return StepResult.fail("Timed out generating SSH host keys")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        # Start sshd
        try:
            sshd_result = self._run_exec(
                ["/usr/sbin/sshd"], timeout=15
            )
            if sshd_result.returncode != 0:
                return StepResult.fail(
                    "Failed to start sshd: {}".format(
                        sshd_result.stderr.strip()
                    )
                )
        except subprocess.TimeoutExpired:
            return StepResult.fail("Timed out starting sshd")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        return StepResult.success(
            "Container '{}' started with sshd running".format(self.name)
        )

    def wait_for_ssh(self, timeout=120):
        # type: (int) -> StepResult
        """Wait until SSH is reachable inside the container.

        Obtains the container IP via docker inspect and polls SSH
        every 5 seconds until it responds or the timeout elapses.

        Args:
            timeout: Maximum seconds to wait for SSH.

        Returns:
            StepResult with the container IP address.
        """
        # Get container IP
        try:
            result = subprocess.run(
                [
                    "docker", "inspect", "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    self.name,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return StepResult.fail("docker inspect timed out")
        except FileNotFoundError:
            return StepResult.fail("Docker CLI not found")

        if result.returncode != 0:
            return StepResult.fail(
                "Failed to get container IP: {}".format(result.stderr.strip())
            )

        ip = result.stdout.strip()
        if not ip:
            return StepResult.fail(
                "Container '{}' has no IP address assigned".format(self.name)
            )

        self.ip = ip

        # Poll SSH
        deadline = time.time() + timeout
        last_error = ""

        while time.time() < deadline:
            try:
                ssh_result = subprocess.run(
                    [
                        "ssh",
                        "-o", "ConnectTimeout=3",
                        "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=accept-new",
                        "root@{}".format(ip),
                        "echo", "ok",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if ssh_result.returncode == 0:
                    return StepResult.success(
                        "SSH available at root@{ip}".format(ip=ip)
                    )
                last_error = ssh_result.stderr.strip()
            except subprocess.TimeoutExpired:
                last_error = "SSH connection timed out"
            except FileNotFoundError:
                return StepResult.fail(
                    "ssh client not found in PATH"
                )

            time.sleep(5)

        return StepResult.fail(
            "SSH not reachable at {} after {}s: {}".format(
                ip, timeout, last_error
            )
        )

    def get_ip(self):
        # type: () -> Optional[str]
        """Return the container's IP address.

        Returns:
            IP address string, or None if not yet determined.
        """
        return self.ip

    def cleanup_on_failure(self):
        # type: () -> StepResult
        """Stop and remove the container, ignoring errors.

        Used to clean up after a failed provisioning attempt.

        Returns:
            StepResult indicating cleanup was attempted.
        """
        errors = []  # type: List[str]

        # Stop container (ignore errors)
        try:
            result = subprocess.run(
                ["docker", "stop", self.name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                errors.append(
                    "stop: {}".format(result.stderr.strip())
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Remove container (ignore errors)
        try:
            result = subprocess.run(
                ["docker", "rm", self.name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                errors.append(
                    "rm: {}".format(result.stderr.strip())
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        if errors:
            return StepResult.success(
                "Cleanup attempted with warnings: {}".format("; ".join(errors))
            )

        return StepResult.success(
            "Container '{}' stopped and removed".format(self.name)
        )

    def dry_run_summary(self):
        # type: () -> List[str]
        """Return a human-readable summary of what would happen.

        Returns:
            List of description strings for each provisioning step.
        """
        image_desc = self.image if self.image else "auto-detect (gentoo-drone or gentoo/stage3)"
        return [
            "Backend: Docker (local container)",
            "Container name: {}".format(self.name),
            "Image: {}".format(image_desc),
            "Resources: {} CPUs, {} MB RAM".format(self.cores, self.ram_mb),
            "docker create --name {name} --hostname {name} --privileged "
            "--cpus {cores} --memory {ram}m --tmpfs /tmp "
            "-v /var/cache/distfiles:/var/cache/distfiles "
            "--entrypoint /bin/bash {image} -c 'sleep infinity'".format(
                name=self.name,
                cores=self.cores,
                ram=self.ram_mb,
                image=image_desc,
            ),
            "docker start {}".format(self.name),
            "Inject SSH public key into /root/.ssh/authorized_keys",
            "Install and start sshd if not present",
            "Wait for SSH connectivity on Docker bridge network",
        ]
