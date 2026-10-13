"""The snapshot window must be wide enough for the lag clients actually have.

_store_snapshot kept 5 snapshots (~10s of feed versions). A client whose
`since` is older than the oldest snapshot finds no base to diff against and
gets a full snapshot instead.

Measured against a full day of production traffic (24 Sep 2026, 786 717
?since= requests): full responses had a lag of p50 7s, p90 30s; delta
responses p50 5s, p90 9s. The boundary sat exactly at the 10s window.

Clients fall behind because of the staleness guard in map.html: a response at
or behind the rendered timestamp is dropped without advancing _lastVehicleTs,
so that poll is wasted and the lag grows by one interval. With three workers
holding feed versions ~6s apart, a client pinned to the highest timestamp it
has seen discards every answer from a worker that is behind.

30 snapshots covers ~60s, comfortably past the measured p90. Cost measured on
the live box: ~0.2 MB per snapshot at 1153 vehicles, so ~5.9 MB per worker and
~17.7 MB across three, against 4 GB free.
"""

import pytest

import server


def _vehicle(vid, lat=59.33):
    return {"id": vid, "lat": lat, "lon": 18.07, "bearing": 0, "speed": 0,
            "line": "19", "headsign": "Hagsätra", "route_id": "r19",
            "trip_id": "t1", "transport_mode": "METRO", "metro_group": "green",
            "status": "IN_TRANSIT_TO", "timestamp": 1000}


def _response(vehicles, ts):
    return {"timestamp": ts, "vehicles": vehicles}


@pytest.fixture(autouse=True)
def _clean():
    with server._prev_vehicles_lock:
        server._prev_vehicles.clear()
    yield
    with server._prev_vehicles_lock:
        server._prev_vehicles.clear()


def test_window_is_declared_as_a_named_constant():
    """A bare 5 in the eviction loop is how this stayed unnoticed."""
    assert server._SNAPSHOT_HISTORY == 30


def test_thirty_snapshots_are_retained():
    for i in range(30):
        server._store_snapshot(_response([_vehicle("a")], ts=1000 + i))
    with server._prev_vehicles_lock:
        assert len(server._prev_vehicles) == 30
        assert 1000 in server._prev_vehicles, "oldest of 30 must survive"


def test_oldest_is_evicted_beyond_the_window():
    for i in range(31):
        server._store_snapshot(_response([_vehicle("a")], ts=1000 + i))
    with server._prev_vehicles_lock:
        assert len(server._prev_vehicles) == 30
        assert 1000 not in server._prev_vehicles
        assert 1030 in server._prev_vehicles


def test_a_client_a_minute_behind_still_gets_a_delta(monkeypatch):
    """The regression this exists for: ~60s of lag must not force a full
    snapshot. At 5 snapshots this client fell outside the window entirely."""
    for i in range(30):
        server._store_snapshot(_response([_vehicle("a", lat=59.30)], ts=1000 + i * 2))

    current = _response([_vehicle("a", lat=59.40)], ts=1060)
    monkeypatch.setattr(server, "_fetch_vehicle_positions", lambda: current)

    diff = server._compute_vehicle_diff(1002)   # 58s behind the newest

    assert diff is not None, "a client 58s behind must still get a delta"
    assert diff["ts"] == 1060
    assert [u["id"] for u in diff["u"]] == ["a"]
    assert diff["u"][0]["lat"] == 59.40


def test_storing_the_current_version_can_evict_the_clients_base(monkeypatch):
    """A real edge, pinned so nobody mistakes it for a bug later.

    _compute_vehicle_diff stores the current feed version before looking up
    the base, so the usable history is _SNAPSHOT_HISTORY - 1 versions, not
    _SNAPSHOT_HISTORY. A client sitting exactly on the boundary is pushed out
    by its own request and gets a full snapshot. Widening the window moves
    this edge; it does not remove it.
    """
    for i in range(30):
        server._store_snapshot(_response([_vehicle("a")], ts=1000 + i * 2))

    current = _response([_vehicle("a", lat=59.40)], ts=1060)
    monkeypatch.setattr(server, "_fetch_vehicle_positions", lambda: current)

    assert server._compute_vehicle_diff(1000) is None
