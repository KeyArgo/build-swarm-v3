# Build Swarm v3 -- Quickstart Guide

Get a distributed Gentoo binary package builder running from scratch.

**Time to first build**: ~20 minutes (control plane + one drone)

---

## Prerequisites

- A **Gentoo Linux** machine to run the control plane (e.g., argobox-lite at 10.0.0.199)
- One or more **Gentoo machines** to act as build drones (LXC containers, VMs, or bare metal)
- **SSH root access** from the control plane host to each drone
- **Python 3.8+** on the control plane host

---

## Step 1: Install the Control Plane (5 minutes)

### On the control plane host:

```bash
# Clone the repo
git clone https://git.argobox.com/KeyArgo/build-swarm-v3.git
cd build-swarm-v3

# Option A: Install as a command (recommended)
pip install -e .

# Option B: Run directly without installing
chmod +x build-swarmv3
./build-swarmv3 serve
```

### Create required directories:

```bash
mkdir -p /var/cache/binpkgs-v3-staging
mkdir -p /var/cache/binpkgs-v3
mkdir -p /var/log/build-swarm-v3
```

### Start the control plane:

```bash
build-swarmv3 serve
```

You should see:

```
Build Swarm v3 Control Plane v3.0.0
Starting server on port 8100...
```

### Verify it's running:

```bash
curl -s http://localhost:8100/api/v1/status | python3 -m json.tool
```

### (Optional) Install as an OpenRC service:

```bash
cp drone-image/swarm-control-plane.initd /etc/init.d/swarm-control-plane
chmod +x /etc/init.d/swarm-control-plane
rc-update add swarm-control-plane default
rc-service swarm-control-plane start
```

---

## Step 2: Prepare a Drone (10 minutes)

A drone is any Gentoo machine that will compile packages. You can use:

- **Existing Gentoo system** -- any machine with `emerge` available
- **Fresh stage3** -- download from https://www.gentoo.org/downloads/ and extract into an LXC container or VM
- **Golden image clone** -- snapshot a clean drone and clone it (see Step 7)

### Set up SSH access:

From the control plane host, ensure you can SSH as root to the drone:

```bash
# Test SSH connectivity
ssh root@<DRONE_IP> "echo ok"

# If needed, copy your SSH key
ssh-copy-id root@<DRONE_IP>
```

### Deploy the drone:

```bash
# Preview what will change (safe, read-only)
build-swarmv3 drone deploy <DRONE_IP> --name drone-01 --dry-run

# Deploy for real
build-swarmv3 drone deploy <DRONE_IP> --name drone-01
```

This runs the bootstrap script on the target machine, which:

1. Detects the environment (LXC, VM, or bare metal)
2. Sets the Gentoo profile to `amd64/23.0`
3. Installs a minimal `make.conf` with `buildpkg` enabled
4. Sets per-package USE flags for build targets (KDE, Qt6, Wayland, etc.)
5. Syncs the portage tree
6. Sets the @world file to 10 essential packages
7. Updates the system
8. Installs the drone agent and configuration
9. Generates SSH keys (prints the public key for you to add to the control plane)
10. Creates and starts the OpenRC service

### Add the drone's SSH key to the control plane:

The bootstrap output will print a public key. Add it to the control plane host so the drone can upload built packages:

```bash
# On the control plane host
echo "<drone-public-key>" >> ~/.ssh/authorized_keys
```

### Verify the drone registered:

```bash
build-swarmv3 fleet
```

You should see your drone listed as `online`.

### (Optional) Remove bloat from an existing system:

If deploying to a machine that already has extra packages installed:

```bash
build-swarmv3 drone deploy <DRONE_IP> --name drone-01 --prune
```

The `--prune` flag runs `emerge --depclean` to remove packages not in the dependency tree of the 10 world atoms. This can remove hundreds of packages on bloated systems.

---

## Step 3: Run a Test Build (5 minutes)

### Queue a small package:

```bash
build-swarmv3 queue add =app-misc/screen-4.9.1
```

### Watch the build in real-time:

```bash
build-swarmv3 monitor
```

You'll see the package get delegated to a drone, built, and the binary uploaded back to the control plane.

### Check the result:

```bash
build-swarmv3 history
```

A successful build looks like:

```
Time                 Package                    Drone            Status     Duration
2026-02-09 22:15:00  =app-misc/screen-4.9.1    drone-01         success    21.3s
```

---

## Step 4: Full @world Build

To build all packages in your @world set:

```bash
# Queue everything from @world
build-swarmv3 fresh

# Monitor progress
build-swarmv3 monitor
```

This runs `emerge --pretend --emptytree @world` to discover all packages, then queues them for building. With 4 drones, a full @world of ~500 packages takes 2-4 hours depending on what needs compiling.

### Pause and resume:

```bash
# Pause the queue (drones finish current work, then idle)
build-swarmv3 control pause

# Resume
build-swarmv3 control resume
```

---

## Step 5: Audit Your Fleet

Check all drones against the spec:

```bash
# Audit all drones
build-swarmv3 drone audit

# Audit a specific drone
build-swarmv3 drone audit drone-01

# JSON output (for scripting)
build-swarmv3 drone audit --json
```

Output shows PASS/WARN/FAIL for 10 compliance checks:

```
drone-01 (10.0.0.175)  COMPLIANT
  PASS  profile          default/linux/amd64/23.0
  PASS  packages         287 installed (limit: 400)
  PASS  world_file       matches spec exactly
  PASS  forbidden        no forbidden packages found
  PASS  commands         all required commands found
  PASS  directories      all required directories exist
  PASS  files            all required files exist
  PASS  service          swarm-drone is running
  PASS  service          sshd is running
  PASS  make_conf        required FEATURES present
  PASS  portage_tree     0 days old
  (10 pass, 0 warn, 0 fail)
```

---

## Step 6: Switch Between v2 and v3

If you're running both v2 and v3 control planes, you can move drones between them:

```bash
# Move all drones to v3
build-swarmv3 switch v3

# Move all drones back to v2
build-swarmv3 switch v2

# Move specific drones
build-swarmv3 switch v3 drone-01 drone-02

# Preview without making changes
build-swarmv3 switch v3 --dry-run
```

---

## Step 7: Golden Image Strategy

Once you have a clean, compliant drone, snapshot it as a template for fast deployment.

### Create the golden image:

```bash
# 1. Deploy a clean drone
build-swarmv3 drone deploy <IP> --name drone-golden

# 2. Verify it passes all checks
build-swarmv3 drone audit drone-golden

# 3. Snapshot it (Proxmox example)
pct stop <VMID>
pct snapshot <VMID> golden-v1 --description "Clean drone image 2026-02-09"

# Or for QEMU VMs:
qm stop <VMID>
qm snapshot <VMID> golden-v1 --description "Clean drone image 2026-02-09"
```

### Deploy from the golden image:

```bash
# Clone the template (Proxmox example)
pct clone <TEMPLATE_VMID> <NEW_VMID> --hostname drone-02
pct start <NEW_VMID>

# Update the drone name and control plane URL
build-swarmv3 drone deploy <NEW_IP> --name drone-02
```

### Keep the golden image fresh:

Re-snapshot after portage syncs or package updates. The compliance audit tells you if a drone has drifted from the spec.

---

## Step 8: Prevent Drift

Every drone deployed via bootstrap includes a daily compliance check that logs to `/var/log/build-swarm/compliance.log`. If a drone drifts from the spec, it shows up in the log.

### Manual check:

```bash
# Run on the drone itself
/opt/build-swarm/comply-cron.sh
```

### From the control plane:

```bash
# Audit all drones remotely
build-swarmv3 drone audit
```

### Rules to prevent drift:

1. **Never install packages directly on a drone** -- only the 10 @world atoms should be installed
2. **Never change the profile** -- it must stay at `amd64/23.0`
3. **Never modify make.conf** -- the spec defines the required FEATURES
4. **If a drone drifts**, redeploy it: `build-swarmv3 drone deploy <IP> --prune`
5. **If a drone is severely drifted**, deploy a fresh one from the golden image

---

## CLI Reference

| Command | Purpose |
|---------|---------|
| `build-swarmv3 serve` | Start the control plane server |
| `build-swarmv3 status` | Show queue status |
| `build-swarmv3 fresh` | Queue all @world packages |
| `build-swarmv3 queue add <pkgs>` | Add specific packages to the queue |
| `build-swarmv3 queue list` | List queue contents |
| `build-swarmv3 fleet` | List registered drones |
| `build-swarmv3 history` | Show build history |
| `build-swarmv3 control <action>` | Control actions: pause, resume, unblock, reset, rebalance |
| `build-swarmv3 monitor` | Live status display |
| `build-swarmv3 drone audit` | Audit drones against spec |
| `build-swarmv3 drone deploy <ip>` | Deploy drone to target machine |
| `build-swarmv3 switch <v2\|v3>` | Switch drones between control planes |

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SWARMV3_URL` | `http://localhost:8100` | Control plane URL (for CLI commands) |
| `CONTROL_PLANE_PORT` | `8100` | Port for `serve` command |
| `SWARM_DB_PATH` | `/var/lib/build-swarm-v3/swarm.db` | SQLite database path |
| `STAGING_PATH` | `/var/cache/binpkgs-v3-staging` | Binary package staging directory |
| `BINHOST_PATH` | `/var/cache/binpkgs-v3` | Final binary package directory |
| `GATEWAY_HOST` | `10.0.0.199` | Control plane host (for switch/audit) |
| `V2_GATEWAY_PORT` | `8090` | V2 gateway port |
| `V3_GATEWAY_PORT` | `8100` | V3 control plane port |

---

## Troubleshooting

### "Cannot connect to control plane"
```bash
# Is it running?
pgrep -f "build-swarmv3 serve"

# Check the port
ss -tlnp | grep 8100

# Check the log
tail -f /var/log/build-swarm-v3/control-plane.log
```

### Drone not appearing in fleet
```bash
# Can you SSH to it?
ssh root@<DRONE_IP> "echo ok"

# Is the drone service running?
ssh root@<DRONE_IP> "rc-service swarm-drone status"

# Check the drone log
ssh root@<DRONE_IP> "tail -20 /var/log/build-swarm/drone.log"

# Check the drone config
ssh root@<DRONE_IP> "cat /etc/build-swarm/drone.conf"
```

### Builds failing with "missing_binary"
```bash
# Check if the staging directory exists on the control plane
ls -la /var/cache/binpkgs-v3-staging/

# Check if the drone can rsync to the control plane
ssh root@<DRONE_IP> "rsync --dry-run /tmp/test root@<CP_IP>:/tmp/"

# The drone's SSH key must be in the control plane's authorized_keys
```

### Drone audit shows failures
```bash
# Get full details
build-swarmv3 drone audit <drone-name>

# Fix by redeploying
build-swarmv3 drone deploy <IP> --name <drone-name> --prune
```
