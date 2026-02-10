"""Tests for the protocol logger module."""

import json
import time
import pytest
from pathlib import Path

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarm.db import SwarmDB
from swarm import protocol_logger


@pytest.fixture
def db(tmp_path):
    """Create a fresh database for each test."""
    db_path = str(tmp_path / 'test.db')
    return SwarmDB(db_path)


class TestClassifyMessage:
    """Test message type classification."""

    def test_work_request(self):
        assert protocol_logger.classify_message('GET', '/api/v1/work') == 'work_request'

    def test_work_request_with_params(self):
        assert protocol_logger.classify_message('GET', '/api/v1/work?id=abc123') == 'work_request'

    def test_register(self):
        assert protocol_logger.classify_message('POST', '/api/v1/register') == 'register'

    def test_complete(self):
        assert protocol_logger.classify_message('POST', '/api/v1/complete') == 'complete'

    def test_discovery(self):
        assert protocol_logger.classify_message('GET', '/api/v1/orchestrator') == 'discovery'

    def test_status_query(self):
        assert protocol_logger.classify_message('GET', '/api/v1/status') == 'status_query'

    def test_events_query(self):
        assert protocol_logger.classify_message('GET', '/api/v1/events') == 'events_query'

    def test_queue(self):
        assert protocol_logger.classify_message('POST', '/api/v1/queue') == 'queue'

    def test_control(self):
        assert protocol_logger.classify_message('POST', '/api/v1/control') == 'control'

    def test_node_list(self):
        assert protocol_logger.classify_message('GET', '/api/v1/nodes') == 'node_list'

    def test_health_check(self):
        assert protocol_logger.classify_message('GET', '/api/v1/health') == 'health_check'

    def test_node_pause(self):
        assert protocol_logger.classify_message('POST', '/api/v1/nodes/abc123/pause') == 'node_pause'

    def test_node_resume(self):
        assert protocol_logger.classify_message('POST', '/api/v1/nodes/abc123/resume') == 'node_resume'

    def test_node_delete(self):
        assert protocol_logger.classify_message('DELETE', '/api/v1/nodes/abc123') == 'node_delete'

    def test_trailing_slash(self):
        assert protocol_logger.classify_message('GET', '/api/v1/health/') == 'health_check'

    def test_unknown(self):
        assert protocol_logger.classify_message('GET', '/api/v1/unknown-endpoint') == 'unknown'

    def test_protocol_query(self):
        assert protocol_logger.classify_message('GET', '/api/v1/protocol') == 'protocol_query'

    def test_provisioning(self):
        assert protocol_logger.classify_message('GET', '/api/v1/provision/bootstrap') == 'provisioning'


class TestWriteBehind:
    """Test the write-behind queue mechanism."""

    def test_init_and_drain(self, db):
        """Test that init starts the writer and entries drain to DB."""
        protocol_logger.init(db)
        try:
            protocol_logger.log_request(
                source_ip='10.0.0.100',
                method='GET',
                path='/api/v1/health',
                req_body=None,
                resp_body='{"status":"ok"}',
                status_code=200,
                latency_ms=1.5,
            )
            # Wait for writer to drain
            time.sleep(1.5)
            rows = db.fetchall("SELECT * FROM protocol_log")
            assert len(rows) >= 1
            entry = dict(rows[0])
            assert entry['msg_type'] == 'health_check'
            assert entry['status_code'] == 200
            assert entry['latency_ms'] == 1.5
            assert entry['source_ip'] == '10.0.0.100'
        finally:
            protocol_logger.shutdown()

    def test_protocol_query_excluded(self, db):
        """Protocol query requests should NOT be logged (prevents recursion)."""
        protocol_logger.init(db)
        try:
            protocol_logger.log_request(
                source_ip='10.0.0.100',
                method='GET',
                path='/api/v1/protocol',
                req_body=None,
                resp_body='{"entries":[]}',
                status_code=200,
                latency_ms=2.0,
            )
            time.sleep(1.5)
            rows = db.fetchall("SELECT * FROM protocol_log")
            assert len(rows) == 0
        finally:
            protocol_logger.shutdown()

    def test_batch_insert(self, db):
        """Multiple entries should batch-insert correctly."""
        protocol_logger.init(db)
        try:
            for i in range(10):
                protocol_logger.log_request(
                    source_ip='10.0.0.100',
                    method='GET',
                    path='/api/v1/health',
                    req_body=None,
                    resp_body='{"status":"ok"}',
                    status_code=200,
                    latency_ms=float(i),
                )
            time.sleep(1.5)
            count = db.fetchval("SELECT COUNT(*) FROM protocol_log")
            assert count == 10
        finally:
            protocol_logger.shutdown()


class TestFieldExtraction:
    """Test field extraction for different message types."""

    def test_work_request_fields(self, db):
        protocol_logger.init(db)
        try:
            protocol_logger.log_request(
                source_ip='10.0.0.201',
                method='GET',
                path='/api/v1/work?id=drone-izar',
                req_body=None,
                resp_body='{"package":"=dev-libs/foo-1.0"}',
                status_code=200,
                latency_ms=5.0,
            )
            time.sleep(1.5)
            entry = dict(db.fetchone("SELECT * FROM protocol_log LIMIT 1"))
            assert entry['drone_id'] == 'drone-izar'
            assert entry['package'] == '=dev-libs/foo-1.0'
            assert entry['msg_type'] == 'work_request'
        finally:
            protocol_logger.shutdown()

    def test_register_fields(self, db):
        protocol_logger.init(db)
        try:
            req = json.dumps({
                'id': 'abc123',
                'name': 'drone-tarn',
                'ip': '10.0.0.175',
                'capabilities': {'cores': 14}
            })
            protocol_logger.log_request(
                source_ip='10.0.0.175',
                method='POST',
                path='/api/v1/register',
                req_body=req,
                resp_body='{"status":"registered"}',
                status_code=200,
                latency_ms=3.0,
            )
            time.sleep(1.5)
            entry = dict(db.fetchone("SELECT * FROM protocol_log LIMIT 1"))
            assert entry['drone_id'] == 'abc123'
            assert entry['source_node'] == 'drone-tarn'
            assert 'cores=14' in entry['request_summary']
        finally:
            protocol_logger.shutdown()

    def test_complete_fields(self, db):
        protocol_logger.init(db)
        try:
            req = json.dumps({
                'id': 'drone-izar',
                'package': '=dev-qt/qtbase-6.10.1',
                'status': 'success',
                'build_duration_s': 45.2,
            })
            protocol_logger.log_request(
                source_ip='10.0.0.201',
                method='POST',
                path='/api/v1/complete',
                req_body=req,
                resp_body='{"status":"ok","accepted":true}',
                status_code=200,
                latency_ms=2.0,
            )
            time.sleep(1.5)
            entry = dict(db.fetchone("SELECT * FROM protocol_log LIMIT 1"))
            assert entry['drone_id'] == 'drone-izar'
            assert entry['package'] == '=dev-qt/qtbase-6.10.1'
            assert 'status=success' in entry['request_summary']
        finally:
            protocol_logger.shutdown()


class TestQueries:
    """Test query functions."""

    def _insert_entries(self, db):
        """Insert test protocol entries directly."""
        now = time.time()
        entries = [
            (now - 10, '10.0.0.201', 'drone-izar', 'GET', '/api/v1/work',
             'work_request', 'drone-izar', '=dev-libs/foo-1.0', None, 200,
             'GET /work', '200 pkg=foo', None, None, 5.0, 100),
            (now - 8, '10.0.0.175', 'drone-tarn', 'POST', '/api/v1/register',
             'register', 'tarn-id', None, None, 200,
             'REGISTER drone-tarn', '200 registered', None, None, 3.0, 50),
            (now - 5, '10.0.0.201', 'drone-izar', 'POST', '/api/v1/complete',
             'complete', 'drone-izar', '=dev-libs/foo-1.0', None, 200,
             'COMPLETE foo', '200 ok', None, None, 2.0, 80),
            (now - 3, '10.0.0.100', None, 'GET', '/api/v1/status',
             'status_query', None, None, None, 200,
             'GET /status', '200 N=10 D=2 R=5', None, None, 15.0, 2000),
            (now - 1, '10.0.0.100', None, 'GET', '/api/v1/health',
             'health_check', None, None, None, 200,
             'GET /health', '200 ok', None, None, 0.5, 50),
        ]
        db.executemany("""
            INSERT INTO protocol_log
                (timestamp, source_ip, source_node, method, path, msg_type,
                 drone_id, package, session_id, status_code,
                 request_summary, response_summary,
                 request_body, response_body, latency_ms, content_length)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, entries)

    def test_get_entries_all(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db)
        assert len(entries) == 5

    def test_get_entries_by_type(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db, msg_type='work_request')
        assert len(entries) == 1
        assert entries[0]['msg_type'] == 'work_request'

    def test_get_entries_by_drone(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db, drone_id='drone-izar')
        assert len(entries) == 2  # work_request + complete

    def test_get_entries_by_package(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db, package='foo')
        assert len(entries) == 2  # work_request + complete with foo

    def test_get_entries_min_latency(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db, min_latency=10.0)
        assert len(entries) == 1
        assert entries[0]['msg_type'] == 'status_query'

    def test_get_entries_since_id(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db, since_id=3)
        assert len(entries) == 2  # entries 4 and 5

    def test_get_entries_limit(self, db):
        self._insert_entries(db)
        entries = protocol_logger.get_protocol_entries(db, limit=2)
        assert len(entries) == 2

    def test_get_detail(self, db):
        self._insert_entries(db)
        detail = protocol_logger.get_protocol_detail(db, 1)
        assert detail is not None
        assert detail['msg_type'] == 'work_request'

    def test_get_detail_not_found(self, db):
        self._insert_entries(db)
        detail = protocol_logger.get_protocol_detail(db, 999)
        assert detail is None

    def test_get_stats(self, db):
        self._insert_entries(db)
        stats = protocol_logger.get_protocol_stats(db)
        assert stats['total'] == 5
        types = {t['msg_type']: t['count'] for t in stats['by_type']}
        assert types.get('work_request') == 1
        assert types.get('health_check') == 1

    def test_get_stats_since(self, db):
        self._insert_entries(db)
        stats = protocol_logger.get_protocol_stats(db, since=time.time() - 4)
        # Only entries from last 4 seconds (status_query + health_check)
        assert stats['total'] == 2


class TestDensity:
    """Test activity density for replay scrubber."""

    def test_density(self, db):
        now = time.time()
        # Insert 20 entries spread over 100 seconds
        entries = []
        for i in range(20):
            ts = now - 100 + (i * 5)
            entries.append((ts, '10.0.0.1', None, 'GET', '/api/v1/health',
                           'health_check', None, None, None, 200,
                           '', '', None, None, 1.0, 50))
        db.executemany("""
            INSERT INTO protocol_log
                (timestamp, source_ip, source_node, method, path, msg_type,
                 drone_id, package, session_id, status_code,
                 request_summary, response_summary,
                 request_body, response_body, latency_ms, content_length)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, entries)

        density = protocol_logger.get_activity_density(db, now - 100, now, buckets=10)
        assert len(density) == 10
        assert sum(density) == 20


class TestPruning:
    """Test old entry pruning."""

    def test_prune(self, db):
        old_ts = time.time() - (25 * 3600)  # 25 hours ago
        new_ts = time.time() - 60  # 1 minute ago
        db.execute("""
            INSERT INTO protocol_log
                (timestamp, source_ip, method, path, msg_type, status_code, latency_ms)
            VALUES (?, '10.0.0.1', 'GET', '/api/v1/health', 'health_check', 200, 1.0)
        """, (old_ts,))
        db.execute("""
            INSERT INTO protocol_log
                (timestamp, source_ip, method, path, msg_type, status_code, latency_ms)
            VALUES (?, '10.0.0.1', 'GET', '/api/v1/health', 'health_check', 200, 1.0)
        """, (new_ts,))

        count_before = db.fetchval("SELECT COUNT(*) FROM protocol_log")
        assert count_before == 2

        protocol_logger.prune_old_entries(db, max_age_hours=24)

        count_after = db.fetchval("SELECT COUNT(*) FROM protocol_log")
        assert count_after == 1


class TestStateReconstruction:
    """Test state-at-time reconstruction."""

    def test_state_at_time(self, db):
        now = time.time()
        status_resp = json.dumps({'needed': 10, 'delegated': 2, 'received': 5})
        nodes_resp = json.dumps({'drones': [{'name': 'drone-izar', 'status': 'online'}]})

        db.execute("""
            INSERT INTO protocol_log
                (timestamp, source_ip, method, path, msg_type, status_code,
                 request_summary, response_summary, response_body, latency_ms)
            VALUES (?, '10.0.0.100', 'GET', '/api/v1/status', 'status_query', 200,
                    'GET /status', '200', ?, 5.0)
        """, (now - 10, status_resp))
        db.execute("""
            INSERT INTO protocol_log
                (timestamp, source_ip, method, path, msg_type, status_code,
                 request_summary, response_summary, response_body, latency_ms)
            VALUES (?, '10.0.0.100', 'GET', '/api/v1/nodes', 'node_list', 200,
                    'GET /nodes', '200', ?, 3.0)
        """, (now - 8, nodes_resp))

        state = protocol_logger.get_state_at_time(db, now)
        assert state['status']['needed'] == 10
        assert len(state['nodes']['drones']) == 1
