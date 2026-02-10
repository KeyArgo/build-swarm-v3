"""
Tests for SwarmDB — the SQLite database layer for Build Swarm v3.

Uses pytest with tmp_path fixture so every test gets a fresh, isolated database.
No production paths are touched.
"""

import time
import pytest
import sys
from pathlib import Path

# Ensure the project root is on the path so `swarm.db` can be imported.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from swarm.db import SwarmDB


# ── Helpers ──────────────────────────────────────────────────────────────


def make_db(tmp_path) -> SwarmDB:
    """Create a SwarmDB backed by a temp-directory SQLite file."""
    return SwarmDB(str(tmp_path / "test.db"))


def register_drone(db: SwarmDB, drone_id: str = "drone-1",
                   name: str = "atlas", ip: str = "10.0.0.50",
                   **kwargs) -> dict:
    """Convenience wrapper to register a drone with sensible defaults."""
    return db.upsert_node(
        node_id=drone_id,
        name=name,
        ip=ip,
        node_type="drone",
        cores=kwargs.get("cores", 8),
        ram_gb=kwargs.get("ram_gb", 16.0),
        capabilities=kwargs.get("capabilities"),
        metrics=kwargs.get("metrics"),
        current_task=kwargs.get("current_task"),
        version=kwargs.get("version", "3.0.0"),
    )


# ── 1. Schema Initialization ────────────────────────────────────────────


def test_init_schema(tmp_path):
    """Creating a SwarmDB initializes all expected tables."""
    db = make_db(tmp_path)
    expected_tables = {
        "nodes", "queue", "build_history", "sessions",
        "config", "drone_health", "metrics_log",
    }
    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    actual = {row["name"] for row in rows}
    assert expected_tables.issubset(actual), (
        f"Missing tables: {expected_tables - actual}"
    )
    db.close()


# ── 2. Node Upsert (Register) ───────────────────────────────────────────


def test_upsert_node(tmp_path):
    """Registering a drone stores it and returns status 'registered'."""
    db = make_db(tmp_path)
    result = register_drone(db)
    assert result == {"status": "registered", "id": "drone-1"}

    node = db.get_node("drone-1")
    assert node is not None
    assert node["name"] == "atlas"
    assert node["ip"] == "10.0.0.50"
    assert node["type"] == "drone"
    assert node["status"] == "online"
    assert node["cores"] == 8
    assert node["ram_gb"] == 16.0
    assert node["online"] is True
    assert node["paused"] is False
    db.close()


# ── 3. Node Upsert Update ───────────────────────────────────────────────


def test_upsert_node_update(tmp_path):
    """Re-registering the same drone updates fields instead of creating a duplicate."""
    db = make_db(tmp_path)
    register_drone(db, drone_id="drone-1", name="atlas", ip="10.0.0.50",
                   cores=8, ram_gb=16.0, version="3.0.0")

    # Second registration with updated values
    db.upsert_node(
        node_id="drone-1", name="atlas", ip="10.0.0.60",
        node_type="drone", cores=16, ram_gb=32.0, version="3.1.0",
    )

    nodes = db.get_all_nodes(include_offline=True)
    assert len(nodes) == 1, "Should still be one node, not two"

    node = db.get_node("drone-1")
    assert node["ip"] == "10.0.0.60"
    assert node["cores"] == 16
    assert node["ram_gb"] == 32.0
    assert node["version"] == "3.1.0"
    db.close()


# ── 4. Get All Nodes ────────────────────────────────────────────────────


def test_get_all_nodes(tmp_path):
    """Multiple drones are listed and filterable by type."""
    db = make_db(tmp_path)
    register_drone(db, "d1", "atlas", "10.0.0.50")
    register_drone(db, "d2", "helios", "10.0.0.51")
    db.upsert_node("s1", "sweeper-1", "10.0.0.52", node_type="sweeper")

    all_nodes = db.get_all_nodes(include_offline=True)
    assert len(all_nodes) == 3

    drones_only = db.get_all_nodes(include_offline=True, node_type="drone")
    assert len(drones_only) == 2
    assert all(n["type"] == "drone" for n in drones_only)

    sweepers = db.get_all_nodes(include_offline=True, node_type="sweeper")
    assert len(sweepers) == 1
    assert sweepers[0]["name"] == "sweeper-1"
    db.close()


# ── 5. Node Offline Detection ───────────────────────────────────────────


def test_node_offline_detection(tmp_path):
    """A node whose last_seen is older than the timeout is marked offline."""
    db = make_db(tmp_path)
    register_drone(db)

    # Manually push last_seen into the past (60 seconds ago)
    old_ts = time.time() - 60
    db.execute("UPDATE nodes SET last_seen = ? WHERE id = ?", (old_ts, "drone-1"))

    # With a 30-second timeout the node should go offline
    db.update_node_status(timeout_seconds=30)

    node = db.get_node("drone-1")
    assert node["status"] == "offline"
    assert node["online"] is False
    db.close()


# ── 6. Queue Packages ───────────────────────────────────────────────────


def test_queue_packages(tmp_path):
    """Queuing packages creates entries and counts are correct."""
    db = make_db(tmp_path)
    added = db.queue_packages(["dev-libs/foo", "dev-libs/bar", "sys-apps/baz"])
    assert added == 3

    counts = db.get_queue_counts()
    assert counts["needed"] == 3
    assert counts["total"] == 3
    assert counts["delegated"] == 0
    assert counts["received"] == 0
    db.close()


# ── 7. Queue Deduplication ──────────────────────────────────────────────


def test_queue_dedup(tmp_path):
    """Queuing the same package twice does not create a duplicate."""
    db = make_db(tmp_path)
    first = db.queue_packages(["dev-libs/foo"])
    second = db.queue_packages(["dev-libs/foo"])
    assert first == 1
    assert second == 0

    counts = db.get_queue_counts()
    assert counts["needed"] == 1
    assert counts["total"] == 1
    db.close()


# ── 8. Assign Package ───────────────────────────────────────────────────


def test_assign_package(tmp_path):
    """A queued package can be assigned to a drone."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["dev-libs/foo"])

    needed = db.get_needed_packages()
    assert len(needed) == 1
    queue_id = needed[0]["id"]

    ok = db.assign_package(queue_id, "drone-1")
    assert ok is True

    counts = db.get_queue_counts()
    assert counts["needed"] == 0
    assert counts["delegated"] == 1

    delegated = db.get_delegated_packages(drone_id="drone-1")
    assert len(delegated) == 1
    assert delegated[0]["package"] == "dev-libs/foo"
    db.close()


# ── 9. Complete Package (Success) ────────────────────────────────────────


def test_complete_package_success(tmp_path):
    """Full lifecycle: queue -> assign -> complete successfully."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["dev-libs/foo"])

    needed = db.get_needed_packages()
    queue_id = needed[0]["id"]
    db.assign_package(queue_id, "drone-1")

    result = db.complete_package("dev-libs/foo", "drone-1", "success",
                                 duration_seconds=42.5)
    assert result is True

    counts = db.get_queue_counts()
    assert counts["received"] == 1
    assert counts["delegated"] == 0
    assert counts["needed"] == 0
    db.close()


# ── 10. Complete Package (Failure) ───────────────────────────────────────


def test_complete_package_failure(tmp_path):
    """A single failure increments failure_count and returns to 'needed'."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["dev-libs/foo"])

    needed = db.get_needed_packages()
    queue_id = needed[0]["id"]
    db.assign_package(queue_id, "drone-1")

    db.complete_package("dev-libs/foo", "drone-1", "failed",
                        error_message="emerge failed")

    # Should be back to needed with failure_count = 1
    row = db.fetchone("SELECT * FROM queue WHERE package = ?", ("dev-libs/foo",))
    assert row is not None
    assert row["status"] == "needed"
    assert row["failure_count"] == 1
    assert row["assigned_to"] is None
    db.close()


# ── 11. Complete Package (Blocked after 5 failures) ─────────────────────


def test_complete_package_blocked(tmp_path):
    """Five consecutive failures block the package."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["dev-libs/trouble"])

    for i in range(5):
        needed = db.get_needed_packages()
        # On the last iteration the package may already be blocked, but for
        # iterations 0-4 it should still be available (it goes back to needed
        # after each failure until the 5th).
        if not needed:
            break
        queue_id = needed[0]["id"]
        db.assign_package(queue_id, "drone-1")
        db.complete_package("dev-libs/trouble", "drone-1", "failed",
                            error_message=f"attempt {i+1}")

    row = db.fetchone("SELECT * FROM queue WHERE package = ?",
                      ("dev-libs/trouble",))
    assert row["status"] == "blocked"
    assert row["failure_count"] == 5

    blocked = db.get_blocked_packages()
    assert len(blocked) == 1
    assert blocked[0]["package"] == "dev-libs/trouble"
    db.close()


# ── 12. Reclaim Package ─────────────────────────────────────────────────


def test_reclaim_package(tmp_path):
    """A delegated package can be reclaimed back to 'needed'."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["dev-libs/foo"])

    needed = db.get_needed_packages()
    queue_id = needed[0]["id"]
    db.assign_package(queue_id, "drone-1")

    assert db.get_queue_counts()["delegated"] == 1

    ok = db.reclaim_package("dev-libs/foo")
    assert ok is True

    counts = db.get_queue_counts()
    assert counts["needed"] == 1
    assert counts["delegated"] == 0

    # The row should have no assigned_to anymore
    row = db.fetchone("SELECT * FROM queue WHERE package = ?", ("dev-libs/foo",))
    assert row["assigned_to"] is None
    assert row["assigned_at"] is None
    db.close()


# ── 13. Unblock All ─────────────────────────────────────────────────────


def test_unblock_all(tmp_path):
    """All blocked packages are returned to 'needed' with failure counts reset."""
    db = make_db(tmp_path)
    register_drone(db)

    packages = ["dev-libs/a", "dev-libs/b"]
    db.queue_packages(packages)

    # Force both packages into blocked state
    for pkg in packages:
        db.execute(
            "UPDATE queue SET status = 'blocked', failure_count = 5 WHERE package = ?",
            (pkg,))

    assert db.get_queue_counts()["blocked"] == 2

    unblocked = db.unblock_all()
    assert unblocked == 2

    counts = db.get_queue_counts()
    assert counts["blocked"] == 0
    assert counts["needed"] == 2

    # Failure counts should be reset
    for pkg in packages:
        row = db.fetchone("SELECT * FROM queue WHERE package = ?", (pkg,))
        assert row["failure_count"] == 0
    db.close()


# ── 14. Session Lifecycle ───────────────────────────────────────────────


def test_session_lifecycle(tmp_path):
    """Create a session, queue packages with session_id, complete session."""
    db = make_db(tmp_path)
    register_drone(db)

    session = db.create_session("sess-001", name="world-rebuild",
                                total_packages=2)
    assert session["id"] == "sess-001"
    assert session["status"] == "active"

    # Verify it is retrievable
    stored = db.get_session("sess-001")
    assert stored is not None
    assert stored["name"] == "world-rebuild"
    assert stored["total_packages"] == 2

    # active_session should find it
    active = db.get_active_session()
    assert active is not None
    assert active["id"] == "sess-001"

    # Queue packages under this session
    added = db.queue_packages(["dev-libs/foo", "dev-libs/bar"],
                              session_id="sess-001")
    assert added == 2

    session_counts = db.get_queue_counts(session_id="sess-001")
    assert session_counts["needed"] == 2

    # Build both successfully
    for pkg in ["dev-libs/foo", "dev-libs/bar"]:
        needed = db.get_needed_packages(session_id="sess-001")
        queue_id = needed[0]["id"]
        db.assign_package(queue_id, "drone-1")
        db.complete_package(pkg, "drone-1", "success", duration_seconds=10.0)

    session_counts = db.get_queue_counts(session_id="sess-001")
    assert session_counts["received"] == 2
    assert session_counts["needed"] == 0

    # Complete the session
    db.complete_session("sess-001")
    completed = db.get_session("sess-001")
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None
    db.close()


# ── 15. Build History ───────────────────────────────────────────────────


def test_build_history(tmp_path):
    """Completing packages records entries in build_history."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["dev-libs/foo", "dev-libs/bar"])

    # Complete foo (success)
    needed = db.get_needed_packages()
    db.assign_package(needed[0]["id"], "drone-1")
    db.complete_package("dev-libs/foo", "drone-1", "success",
                        duration_seconds=30.0)

    # Complete bar (failed)
    needed = db.get_needed_packages()
    db.assign_package(needed[0]["id"], "drone-1")
    db.complete_package("dev-libs/bar", "drone-1", "failed",
                        error_message="build error")

    history = db.get_build_history()
    assert len(history) == 2

    # History is ordered by built_at DESC, so latest first
    statuses = {h["package"]: h["status"] for h in history}
    assert statuses["dev-libs/foo"] == "success"
    assert statuses["dev-libs/bar"] == "failed"

    # Drone name should be recorded
    for entry in history:
        assert entry["drone_id"] == "drone-1"
        assert entry["drone_name"] == "atlas"
    db.close()


# ── 16. Build Stats ─────────────────────────────────────────────────────


def test_build_stats(tmp_path):
    """Stats correctly calculate success rate and average duration."""
    db = make_db(tmp_path)
    register_drone(db)
    db.queue_packages(["a", "b", "c", "d"])

    # Build 3 successfully (durations: 10, 20, 30) and 1 failure
    for i, pkg in enumerate(["a", "b", "c"]):
        needed = db.get_needed_packages()
        db.assign_package(needed[0]["id"], "drone-1")
        db.complete_package(pkg, "drone-1", "success",
                            duration_seconds=(i + 1) * 10.0)

    needed = db.get_needed_packages()
    db.assign_package(needed[0]["id"], "drone-1")
    db.complete_package("d", "drone-1", "failed", error_message="oops")

    stats = db.get_build_stats()
    assert stats["total_builds"] == 4
    assert stats["successful"] == 3
    assert stats["failed"] == 1
    assert stats["success_rate"] == 75.0
    assert stats["avg_duration_s"] == 20.0      # (10 + 20 + 30) / 3
    assert stats["total_duration_s"] == 60.0    # 10 + 20 + 30
    db.close()


# ── 17. Config Get / Set ────────────────────────────────────────────────


def test_config_get_set(tmp_path):
    """Config key-value round-trips work, including JSON mode."""
    db = make_db(tmp_path)

    # Default when key does not exist
    assert db.get_config("missing") is None
    assert db.get_config("missing", default="fallback") == "fallback"

    # Simple string
    db.set_config("build_mode", "parallel")
    assert db.get_config("build_mode") == "parallel"

    # Overwrite
    db.set_config("build_mode", "serial")
    assert db.get_config("build_mode") == "serial"

    # JSON round-trip
    data = {"max_drones": 4, "retry": True}
    db.set_config_json("scheduler_opts", data)
    loaded = db.get_config_json("scheduler_opts")
    assert loaded == data

    # JSON default
    assert db.get_config_json("nope", default={"x": 1}) == {"x": 1}
    db.close()


# ── 18. Metrics Logging ─────────────────────────────────────────────────


def test_metrics_logging(tmp_path):
    """Metrics are logged and retrievable, with queue snapshots."""
    db = make_db(tmp_path)
    register_drone(db)

    # Queue some packages so the metrics snapshot has non-zero counts
    db.queue_packages(["dev-libs/foo", "dev-libs/bar"])

    db.log_metrics(node_id="drone-1", cpu_percent=45.2,
                   ram_percent=60.1, load_1m=2.5)

    metrics = db.get_metrics(node_id="drone-1")
    assert len(metrics) == 1

    m = metrics[0]
    assert m["node_id"] == "drone-1"
    assert m["cpu_percent"] == 45.2
    assert m["ram_percent"] == 60.1
    assert m["load_1m"] == 2.5
    assert m["queue_needed"] == 2
    assert m["queue_delegated"] == 0

    # Log a second entry with no node filter
    db.log_metrics(cpu_percent=10.0, ram_percent=20.0, load_1m=0.5)
    all_metrics = db.get_metrics()
    assert len(all_metrics) == 2

    # Filtering by since should work
    future = time.time() + 1000
    recent = db.get_metrics(since=future)
    assert len(recent) == 0
    db.close()


# ── 19. Drone Health (Circuit Breaker) ───────────────────────────────────


def test_drone_health(tmp_path):
    """Recording failures increments counter; reset clears it."""
    db = make_db(tmp_path)
    register_drone(db, drone_id="drone-1", name="atlas", ip="10.0.0.50")

    # Fresh drone has zero failures
    health = db.get_drone_health("drone-1")
    assert health["failures"] == 0
    assert health["last_failure"] is None

    # Record three failures
    for _ in range(3):
        health = db.record_drone_failure("drone-1")
    assert health["failures"] == 3
    assert health["last_failure"] is not None

    # Ground the drone
    ground_until = time.time() + 300
    db.ground_drone("drone-1", until=ground_until)
    health = db.get_drone_health("drone-1")
    assert health["grounded_until"] is not None
    assert health["grounded_until"] == pytest.approx(ground_until, abs=1)

    # Mark rebooted
    db.mark_drone_rebooted("drone-1")
    health = db.get_drone_health("drone-1")
    assert health["rebooted"] == 1

    # Reset health
    db.reset_drone_health("drone-1")
    health = db.get_drone_health("drone-1")
    assert health["failures"] == 0
    assert health["rebooted"] == 0
    assert health["grounded_until"] is None
    db.close()
