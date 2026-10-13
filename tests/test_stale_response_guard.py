"""The client must drop any /api/vehicles response older than what it shows.

This guard is load-bearing. The server deliberately computes a delta against
the closest older snapshot when it has no exact base (see _compute_vehicle_diff
in server.py), which means a worker running behind the client can answer with a
state the client has already moved past. Nothing on the server stops that any
more — the 2026-09-21 exact-base variant was reverted because it cost ~9.4 GB a
day and the defect it prevented (ghost markers) measured at zero.

So this one line in map.html is the whole protection against vehicles jumping
backwards on the map. Delete it and the jitter comes back silently, with no
server-side net underneath.
"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _map_source():
    return (PROJECT_ROOT / "map.html").read_text(encoding="utf-8")


def test_older_or_equal_response_is_rejected_before_rendering():
    """A response at or behind the rendered timestamp must return early."""
    html = _map_source()
    assert "if (newTs && _lastVehicleTs && newTs <= _lastVehicleTs) return;" in html


def test_guard_runs_before_the_timestamp_is_advanced():
    """Rejecting after assigning _lastVehicleTs would still move it backwards."""
    html = _map_source()
    guard = html.index("newTs <= _lastVehicleTs")
    assign = html.index("_lastVehicleTs = newTs")
    assert guard < assign, (
        "the staleness check must precede the assignment, or a stale response "
        "still rolls the client's clock back"
    )
