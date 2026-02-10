"""
Event ring buffer for Build Swarm v3 activity feed.

Standalone module to avoid circular imports between control_plane, scheduler, health.
"""

import threading
import time

# In-memory ring buffer (max 200 events)
_events = []
_events_lock = threading.Lock()
_event_id = 0


def add_event(event_type: str, message: str, details: dict = None):
    """Append an event to the ring buffer for the activity feed.

    Event types: assign, complete, fail, rebalance, grounded, reclaim,
                 register, offline, queue, control, unblock, return
    """
    global _event_id
    with _events_lock:
        _event_id += 1
        _events.append({
            'id': _event_id,
            'type': event_type,
            'message': message,
            'details': details or {},
            'timestamp': time.time(),
        })
        if len(_events) > 200:
            _events[:] = _events[-200:]


def get_events_since(since_id: int = 0) -> tuple:
    """Get events newer than since_id. Returns (events_list, latest_id)."""
    with _events_lock:
        new = [e for e in _events if e['id'] > since_id]
        return new, _event_id
