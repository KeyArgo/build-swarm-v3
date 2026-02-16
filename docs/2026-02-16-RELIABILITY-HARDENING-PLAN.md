# Build Swarm Reliability Hardening Plan (2026-02-16)

## Context
This document captures the operational issue observed on 2026-02-16:
- delegated packages were repeatedly reclaimed every ~5 minutes as `not-started timeout (>5m)`
- affected drones were then re-assigned the same packages, causing churn loops
- `drone-Trance` triggered repeated level-1 self-healing escalations while still heartbeating

Goal: make Build Swarm behavior conservative, predictable, and distro-grade (Debian/Fedora quality bar) for x86_64 binary production.

## What Was Changed

### 1) Reclaim policy hardened
Implemented in `swarm/scheduler.py`:
- online drones no longer lose delegated work due to `not-started timeout`
- reclaim from online drones only when heartbeat is stale
- reclaim from offline/missing drones remains enabled
- lease reclaim remains enabled, but now tunable and conservative

### 2) Auto-balance behavior improved
Implemented in `swarm/scheduler.py`:
- idle drones can steal queued (not active) work from donors with >1 queued package
- donor retains at least one package
- active work (`building_since`/`current_task`) is never stolen

### 3) Self-healing escalation hardened
Implemented in `swarm/self_healing.py`:
- escalation requires configurable consecutive probe failures (default: 3)
- escalation requires configurable failure time window (default: 180s)
- SSH probe unreachable/timeout does **not** escalate if control-plane heartbeat is fresh
- disk warning remains warning-only

### 4) Conservative defaults made explicit
Implemented in `swarm/config.py`:
- `RECLAIM_OFFLINE_TIMEOUT_MINUTES=15`
- `RECLAIM_LEASE_SECONDS=600`
- `SELF_HEAL_PROBE_INTERVAL_SECONDS=30`
- `SELF_HEAL_MIN_CONSECUTIVE_FAILURES=3`
- `SELF_HEAL_MIN_FAILURE_WINDOW_SECONDS=180`
- `MAX_PREFETCH_PER_DRONE=2`

## New Runtime Controls
Set in environment or `/etc/build-swarm/swarm.conf`:

- `RECLAIM_OFFLINE_TIMEOUT_MINUTES`
  - Reclaim delegated work only when drone heartbeat is stale this long.
  - Default: `15`

- `RECLAIM_LEASE_SECONDS`
  - Lease reclaim threshold for delegated work held by unresponsive nodes.
  - Default: `600`

- `SELF_HEAL_PROBE_INTERVAL_SECONDS`
  - Probe cadence for self-healing checks.
  - Default: `30`

- `SELF_HEAL_MIN_CONSECUTIVE_FAILURES`
  - Failed probes needed before any escalation.
  - Default: `3`

- `SELF_HEAL_MIN_FAILURE_WINDOW_SECONDS`
  - Minimum failure duration before escalation.
  - Default: `180`

- `MAX_PREFETCH_PER_DRONE`
  - Maximum delegated queue depth held by a single drone.
  - Limits over-delegation and reduces assignment churn.
  - Default: `2`

## Fedora/Debian Quality Bar (Target SLOs)

### Availability and Progress
- No reclaim loops on healthy online drones.
- No reclaim of active builds unless heartbeat stale.
- Queue churn rate < 1% of delegated packages per hour during steady-state.
- At least 90% of healthy fleet cores utilized while queue has work.

### Correctness and Safety
- stale completion reports rejected unless package still assigned to that drone.
- split-brain acceptance prevented via assignment validation.
- release promotion blocked by safety gate unless explicitly overridden.

### Recovery Behavior
- self-healing escalates only on persistent health failure.
- no restart storm from probe-only network asymmetry.
- offline drones reclaimed and redistributed automatically.

## Gaps Remaining

1. Probe-path parity
- `drone-Trance` shows SSH probe timeout while API heartbeat is fresh.
- Action: set per-drone SSH route in `drone_config` (reachable IP/port/user/key), preferring Tailscale path.

2. Assignment admission telemetry
- Add explicit `delegated_not_started_age` histogram and per-drone queue age metrics.
- Add alert when median assigned-not-started age exceeds threshold while heartbeat is healthy.

3. Resilience tests
- Add automated tests for:
  - online-not-started reclaim suppression
  - stale-heartbeat reclaim
  - self-heal non-escalation on fresh-heartbeat+SSH-fail
  - escalation after sustained failures only

4. Dual-mothership replication hardening
- Add lag/error budget checks for Mothership-Izar <-> Mothership-Tarn sync and degrade mode runbook.

## Recommended Next Steps

1. Fix SSH probe route for `drone-Trance` (highest impact immediate item).
2. Add resilience unit/integration tests for reclaim/escalation behavior.
3. Add metrics panel for churn/assignment age/lease reclaim causes.
4. Run 24h soak test with active queue and capture SLO report.

## Validation Commands

```bash
# live queue + fleet state
curl -s http://127.0.0.1:8100/api/v1/status | jq

# recent reclaim/escalation/warn events
sqlite3 /home/argo/.local/share/build-swarm-v3/swarm.db \
  "select id,event_type,message,datetime(timestamp,'unixepoch','localtime') from events order by id desc limit 80;"

# service state
sudo rc-service swarm-control-plane status
```

## Change Log (This Session)
- reclaim logic changed from aggressive not-started timeout to heartbeat-based reclaim for online drones
- self-healing changed from fast escalate to persistence-based escalation with heartbeat-aware suppression
- scheduler rebalance policy improved for fairer idle-drone pickup
