"""Tests for /api/vehicles delta/diff support.

P0 — The vehicles endpoint is polled every 1s by every connected client.
Diff responses reduce bandwidth and are critical for user experience.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from google.transit import gtfs_realtime_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vehicle(vid, lat=59.33, lon=18.07, bearing=0, speed=0, **extra):
    """Create a minimal vehicle dict."""
    v = {"id": vid, "lat": lat, "lon": lon, "bearing": bearing, "speed": speed,
         "timestamp": extra.get("timestamp", 1000), "status": "IN_TRANSIT_TO"}
    v.update(extra)
    return v


def _make_response(vehicles, ts=1000):
    """Create a full vehicle response dict."""
    return {"timestamp": ts, "vehicles": vehicles}


@pytest.fixture(autouse=True)
def _clear_snapshots():
    """Reset _prev_vehicles before each test to ensure isolation."""
    import server
    with server._prev_vehicles_lock:
        server._prev_vehicles.clear()
    # Also clear the cache so _fetch_vehicle_positions returns our mock
    server._cache.pop("vehicles", None)
    yield
    with server._prev_vehicles_lock:
        server._prev_vehicles.clear()


# ---------------------------------------------------------------------------
# P0: Full response without since param
# ---------------------------------------------------------------------------

class TestFullResponse:
    """GET /api/vehicles without ?since should always return full data."""

    def test_returns_full_vehicle_list(self, client):
        """Normal poll returns {timestamp, vehicles: [...]}."""
        vehicles = [_make_vehicle("v1"), _make_vehicle("v2")]
        data = _make_response(vehicles, ts=1000)
        with patch("server._fetch_vehicle_positions", return_value=data):
            resp = client.get("/api/vehicles")

        assert resp.status_code == 200
        body = resp.get_json()
        assert "vehicles" in body
        assert "timestamp" in body
        assert len(body["vehicles"]) == 2

    def test_empty_vehicle_list(self, client):
        """No vehicles is still a valid full response."""
        data = _make_response([], ts=1000)
        with patch("server._fetch_vehicle_positions", return_value=data):
            resp = client.get("/api/vehicles")

        body = resp.get_json()
        assert body["vehicles"] == []
        assert body["timestamp"] == 1000

    def test_stores_snapshot_for_future_diffs(self, client):
        """Full response should store a snapshot so subsequent diffs work."""
        import server
        data = _make_response([_make_vehicle("v1")], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=data):
            client.get("/api/vehicles")

        with server._prev_vehicles_lock:
            assert 2000 in server._prev_vehicles


# ---------------------------------------------------------------------------
# P0: Diff response with valid since param
# ---------------------------------------------------------------------------

class TestDiffResponse:
    """GET /api/vehicles?since=<ts> returns a diff when snapshot exists."""

    def test_returns_diff_format(self, client):
        """Diff response has {u, a, r, ts} keys, not 'vehicles'."""
        import server
        v1 = _make_vehicle("v1", lat=59.33)
        v1_moved = _make_vehicle("v1", lat=59.34)

        # Seed a snapshot at ts=1000
        server._store_snapshot(_make_response([v1], ts=1000))

        # Current data has v1 moved
        current = _make_response([v1_moved], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert "u" in body
        assert "a" in body
        assert "r" in body
        assert "ts" in body
        assert "vehicles" not in body

    def test_updated_vehicle_has_changed_fields_only(self, client):
        """Updated vehicles should only contain changed fields + id."""
        import server
        v1 = _make_vehicle("v1", lat=59.33, lon=18.07, bearing=90, speed=10)
        v1_moved = _make_vehicle("v1", lat=59.34, lon=18.07, bearing=90, speed=10)

        server._store_snapshot(_make_response([v1], ts=1000))
        current = _make_response([v1_moved], ts=2000)

        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["id"] == "v1"
        assert delta["lat"] == 59.34
        # Unchanged fields should not be in delta
        assert "lon" not in delta
        assert "bearing" not in delta
        assert "speed" not in delta


# ---------------------------------------------------------------------------
# P0: Fallback to full response for unknown since timestamp
# ---------------------------------------------------------------------------

class TestDiffFallback:
    """When since_ts is unknown, fall back to full response."""

    def test_since_older_than_all_snapshots_returns_full(self, client):
        """When since is older than all stored snapshots, fallback to full.

        _compute_vehicle_diff stores the current snapshot (e.g. ts=3000),
        then looks for snapshots with ts <= since. If since < 3000, no
        match is found, so it returns None and api_vehicles falls through
        to a full response.
        """
        data = _make_response([_make_vehicle("v1")], ts=3000)
        with patch("server._fetch_vehicle_positions", return_value=data):
            resp = client.get("/api/vehicles?since=1")

        body = resp.get_json()
        assert "vehicles" in body
        assert "timestamp" in body

    def test_empty_prev_vehicles_returns_full(self, client):
        """First poll with since param (no snapshots) returns full."""
        data = _make_response([_make_vehicle("v1")], ts=1000)
        with patch("server._fetch_vehicle_positions", return_value=data):
            resp = client.get("/api/vehicles?since=500")

        body = resp.get_json()
        assert "vehicles" in body

    def test_finds_closest_older_snapshot(self, client):
        """If exact ts not found, use closest older snapshot."""
        import server
        v1 = _make_vehicle("v1")
        v2 = _make_vehicle("v2")

        # Store snapshot at ts=1000
        server._store_snapshot(_make_response([v1], ts=1000))

        # Request since=1050 (between snapshots) — should find ts=1000
        current = _make_response([v1, v2], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1050")

        body = resp.get_json()
        # Should get diff format (found older snapshot)
        assert "u" in body
        assert "a" in body
        assert len(body["a"]) == 1  # v2 is new
        assert body["a"][0]["id"] == "v2"


# ---------------------------------------------------------------------------
# P1: Diff with added/removed/updated vehicles
# ---------------------------------------------------------------------------

class TestDiffOperations:
    """Verify that added, removed, and updated vehicles are computed correctly."""

    def test_added_vehicles(self, client):
        """New vehicles appear in the 'a' (added) list."""
        import server
        v1 = _make_vehicle("v1")
        server._store_snapshot(_make_response([v1], ts=1000))

        v2 = _make_vehicle("v2")
        current = _make_response([v1, v2], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        added_ids = [v["id"] for v in body["a"]]
        assert "v2" in added_ids
        assert len(body["r"]) == 0

    def test_removed_vehicles(self, client):
        """Vehicles that disappear appear in the 'r' (removed) list."""
        import server
        v1 = _make_vehicle("v1")
        v2 = _make_vehicle("v2")
        server._store_snapshot(_make_response([v1, v2], ts=1000))

        # Only v1 remains
        current = _make_response([v1], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert "v2" in body["r"]
        assert len(body["a"]) == 0

    def test_updated_vehicles_with_multiple_changes(self, client):
        """Vehicle with lat+bearing changed shows both in delta."""
        import server
        v1 = _make_vehicle("v1", lat=59.33, bearing=90)
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_moved = _make_vehicle("v1", lat=59.34, bearing=180)
        current = _make_response([v1_moved], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["lat"] == 59.34
        assert delta["bearing"] == 180

    def test_mixed_add_remove_update(self, client):
        """Simultaneous adds, removes, and updates in one diff."""
        import server
        v1 = _make_vehicle("v1", lat=59.33)
        v2 = _make_vehicle("v2", lat=59.34)
        server._store_snapshot(_make_response([v1, v2], ts=1000))

        v1_moved = _make_vehicle("v1", lat=59.35)  # updated
        v3 = _make_vehicle("v3")  # added
        # v2 removed
        current = _make_response([v1_moved, v3], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        assert body["u"][0]["id"] == "v1"
        assert len(body["a"]) == 1
        assert body["a"][0]["id"] == "v3"
        assert "v2" in body["r"]


# ---------------------------------------------------------------------------
# P0: Regression — trip switch mid-stream (line/headsign/trip_id/route_id/...)
# ---------------------------------------------------------------------------

class TestTripSwitchRegression:
    """Regression: a vehicle can switch trip mid-stream, changing its line,
    headsign, route_id, trip_id, transport_mode and metro_group while keeping
    the same vehicle id. The delta previously only carried positional fields
    ("lat","lon","bearing","speed","timestamp","status"), so delta clients
    rendered stale line/destination info after a trip switch. The diff key
    tuple was extended to include the trip-identity fields — these tests guard
    that fix.

    User impact (P0): a bus that finishes "Buss 3 mot Karolinska" and starts
    "Buss 3 mot Södersjukhuset" must show the new destination to delta clients,
    not the old one.
    """

    def test_headsign_change_is_in_delta(self, client):
        """A changed headsign must appear in the updated delta (was dropped)."""
        import server
        v1 = _make_vehicle("v1", headsign="Karolinska")
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle("v1", headsign="Södersjukhuset")
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["id"] == "v1"
        assert delta["headsign"] == "Södersjukhuset"

    def test_line_change_is_in_delta(self, client):
        """A changed line designation must appear in the updated delta."""
        import server
        v1 = _make_vehicle("v1", line="3")
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle("v1", line="4")
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        assert body["u"][0]["line"] == "4"

    def test_trip_id_change_is_in_delta(self, client):
        """A changed trip_id must appear in the updated delta."""
        import server
        v1 = _make_vehicle("v1", trip_id="trip-aaa")
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle("v1", trip_id="trip-bbb")
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        assert body["u"][0]["trip_id"] == "trip-bbb"

    def test_full_trip_switch_carries_all_identity_fields(self, client):
        """A complete trip switch changes every trip-identity field at once.

        All six extended fields must be present in the single delta, alongside
        the positional change, with the new values.
        """
        import server
        v1 = _make_vehicle(
            "v1", lat=59.33,
            line="3", headsign="Karolinska", route_id="r-3",
            trip_id="trip-aaa", transport_mode="BUS", metro_group=None,
        )
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle(
            "v1", lat=59.34,
            line="13", headsign="Norsborg", route_id="r-13",
            trip_id="trip-bbb", transport_mode="METRO", metro_group="red",
        )
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["id"] == "v1"
        assert delta["lat"] == 59.34
        assert delta["line"] == "13"
        assert delta["headsign"] == "Norsborg"
        assert delta["route_id"] == "r-13"
        assert delta["trip_id"] == "trip-bbb"
        assert delta["transport_mode"] == "METRO"
        assert delta["metro_group"] == "red"

    def test_only_changed_identity_field_in_delta(self, client):
        """Unchanged trip-identity fields stay out of the delta (no bloat).

        Only headsign changes here; line/route_id/trip_id are identical and
        must NOT appear in the delta even though they are now tracked keys.
        """
        import server
        v1 = _make_vehicle(
            "v1", line="3", headsign="Karolinska",
            route_id="r-3", trip_id="trip-aaa",
        )
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle(
            "v1", line="3", headsign="Södersjukhuset",
            route_id="r-3", trip_id="trip-aaa",
        )
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["headsign"] == "Södersjukhuset"
        assert "line" not in delta
        assert "route_id" not in delta
        assert "trip_id" not in delta

    def test_position_only_change_still_works(self, client):
        """Regression guard: a pure lat/lon move (no trip switch) is unaffected.

        Ensures the extended key tuple did not break the original positional
        delta path. Only lat changes; trip-identity fields are untouched.
        """
        import server
        v1 = _make_vehicle(
            "v1", lat=59.33, lon=18.07,
            line="3", headsign="Karolinska", trip_id="trip-aaa",
        )
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_moved = _make_vehicle(
            "v1", lat=59.34, lon=18.07,
            line="3", headsign="Karolinska", trip_id="trip-aaa",
        )
        current = _make_response([v1_moved], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["lat"] == 59.34
        assert "lon" not in delta
        assert "line" not in delta
        assert "headsign" not in delta
        assert "trip_id" not in delta


# ---------------------------------------------------------------------------
# P1: Empty diff (no changes)
# ---------------------------------------------------------------------------

class TestEmptyDiff:
    """When nothing changed between snapshots, diff lists should be empty."""

    def test_no_changes_returns_empty_diff(self, client):
        """Identical data produces empty u/a/r lists."""
        import server
        v1 = _make_vehicle("v1")
        server._store_snapshot(_make_response([v1], ts=1000))

        # Same vehicle, new timestamp on the response
        current = _make_response([v1], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert body["u"] == []
        assert body["a"] == []
        assert body["r"] == []
        assert body["ts"] == 2000


# ---------------------------------------------------------------------------
# P2: Snapshot rotation (max 5)
# ---------------------------------------------------------------------------

class TestSnapshotRotation:
    """_store_snapshot keeps at most _SNAPSHOT_HISTORY snapshots.

    The window was widened from 5 to 30 on 2026-09-25; see
    tests/test_snapshot_window.py for the measurement behind the number.
    These tests read the constant rather than a literal, so the next change
    to it does not need them edited again.
    """

    def test_window_size_is_capped(self):
        """One past the window evicts the oldest."""
        import server
        n = server._SNAPSHOT_HISTORY
        for i in range(n + 1):
            server._store_snapshot(_make_response(
                [_make_vehicle(f"v{i}")], ts=1000 + i
            ))

        with server._prev_vehicles_lock:
            assert len(server._prev_vehicles) == n
            assert 1000 not in server._prev_vehicles, "oldest should be evicted"
            assert 1000 + n in server._prev_vehicles, "newest should be kept"

    def test_oldest_evicted_first(self):
        """Snapshots are evicted in chronological order, not insertion order."""
        import server
        n = server._SNAPSHOT_HISTORY
        # Fill the window with out-of-order timestamps, 100 the lowest.
        timestamps = [100] + [200 + i * 100 for i in range(n - 1)]
        timestamps = timestamps[1:2] + [timestamps[0]] + timestamps[2:]
        for ts in timestamps:
            server._store_snapshot(_make_response([_make_vehicle("v1")], ts=ts))

        # Add one more to trigger eviction
        server._store_snapshot(_make_response([_make_vehicle("v1")], ts=99999))

        with server._prev_vehicles_lock:
            assert len(server._prev_vehicles) == n
            # ts=100 is the lowest and must go, even though it was not the
            # first one inserted — eviction is chronological, not FIFO.
            assert 100 not in server._prev_vehicles
            assert 99999 in server._prev_vehicles

    def test_no_snapshot_without_timestamp(self):
        """Data without a timestamp should not be stored."""
        import server
        server._store_snapshot({"vehicles": [_make_vehicle("v1")]})

        with server._prev_vehicles_lock:
            assert len(server._prev_vehicles) == 0


# ---------------------------------------------------------------------------
# P0: Delta must clear fields that became empty (omitted from new snapshot)
# ---------------------------------------------------------------------------

class TestDeltaClearsOmittedFields:
    """Payload trim (P2) omits empty-string fields from the vehicle dict, so a
    field that goes from a value to "" simply *disappears* from the current
    snapshot. _compute_vehicle_diff must still tell delta clients to clear the
    stale value by sending the field as "" explicitly (cv.get(key, "")).

    User impact (P0): a bus that had "Buss 3 mot Karolinska" and then loses its
    headsign mid-trip must NOT keep showing the old destination to delta
    clients. Without the explicit "", the field would be absent from the delta
    and the client would render stale data forever.
    """

    def test_headsign_cleared_when_omitted_in_current(self, client):
        """prev has headsign 'X', current omits it → delta carries headsign ''."""
        import server
        v1 = _make_vehicle("v1", headsign="Karolinska")
        server._store_snapshot(_make_response([v1], ts=1000))

        # Current snapshot has the field omitted entirely (as the trim produces)
        v1_new = _make_vehicle("v1")  # no headsign key at all
        v1_new.pop("headsign", None)  # belt-and-suspenders: ensure absent
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        assert len(body["u"]) == 1
        delta = body["u"][0]
        assert delta["id"] == "v1"
        assert "headsign" in delta, "delta must carry the cleared field"
        assert delta["headsign"] == ""

    def test_line_cleared_when_omitted_in_current(self, client):
        """A line that disappears must be sent as '' so the client clears it."""
        import server
        v1 = _make_vehicle("v1", line="3")
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle("v1")
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        delta = body["u"][0]
        assert delta["line"] == ""

    def test_metro_group_cleared_when_omitted_in_current(self, client):
        """metro_group is the most common omitted field — clearing it matters
        when a metro vehicle's trip can no longer be classified as metro."""
        import server
        v1 = _make_vehicle("v1", metro_group="red")
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_new = _make_vehicle("v1")
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        delta = body["u"][0]
        assert delta["metro_group"] == ""

    def test_multiple_identity_fields_cleared_at_once(self, client):
        """A vehicle that loses its whole trip identity clears every field."""
        import server
        v1 = _make_vehicle(
            "v1", line="3", headsign="Karolinska",
            route_id="r-3", trip_id="trip-aaa", metro_group="red",
        )
        server._store_snapshot(_make_response([v1], ts=1000))

        # Trip identity gone — all those keys omitted from current snapshot.
        v1_new = _make_vehicle("v1", lat=59.34)
        current = _make_response([v1_new], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        delta = body["u"][0]
        assert delta["lat"] == 59.34
        assert delta["line"] == ""
        assert delta["headsign"] == ""
        assert delta["route_id"] == ""
        assert delta["trip_id"] == ""
        assert delta["metro_group"] == ""

    def test_unchanged_omitted_field_stays_out_of_delta(self, client):
        """If a field was absent in BOTH snapshots, it must not appear in the
        delta. cv.get(key) == pv.get(key) == None → no entry, no bloat."""
        import server
        # Neither snapshot has metro_group/headsign (e.g. a plain bus).
        v1 = _make_vehicle("v1", lat=59.33, line="3")
        v1.pop("metro_group", None)
        v1.pop("headsign", None)
        server._store_snapshot(_make_response([v1], ts=1000))

        v1_moved = _make_vehicle("v1", lat=59.34, line="3")
        v1_moved.pop("metro_group", None)
        v1_moved.pop("headsign", None)
        current = _make_response([v1_moved], ts=2000)
        with patch("server._fetch_vehicle_positions", return_value=current):
            resp = client.get("/api/vehicles?since=1000")

        body = resp.get_json()
        delta = body["u"][0]
        assert delta["lat"] == 59.34
        assert "metro_group" not in delta
        assert "headsign" not in delta
        assert "line" not in delta  # unchanged value


# ---------------------------------------------------------------------------
# P1: Payload trim — _do_fetch_vehicle_positions omits empty-string fields
# ---------------------------------------------------------------------------

def _feed_with_vehicle(*, vid="bus-1", entity_id="entity-1", lat=59.33,
                       lon=18.07, bearing=90.0, speed=5.0,
                       trip_id="trip-aaa", route_id="route-aaa",
                       feed_ts=1700000000, vehicle_ts=1700000000):
    """Build a serialized GTFS-RT FeedMessage with one vehicle.

    Uses the real protobuf classes (already a server dependency) so the test
    exercises the actual parse path in _do_fetch_vehicle_positions, not a mock
    of it.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_ts
    e = feed.entity.add()
    e.id = entity_id
    vp = e.vehicle
    vp.vehicle.id = vid
    vp.position.latitude = lat
    vp.position.longitude = lon
    vp.position.bearing = bearing
    vp.position.speed = speed
    vp.trip.trip_id = trip_id
    vp.trip.route_id = route_id
    vp.timestamp = vehicle_ts
    return feed.SerializeToString()


def _patched_fetch(monkeypatch, feed_bytes):
    """Point _do_fetch_vehicle_positions at a fake upstream returning feed_bytes
    and ensure a TRAFIKLAB_KEY exists so the early-return guard is skipped."""
    import server
    monkeypatch.setattr(server, "TRAFIKLAB_KEY", "test-key")
    fake_resp = MagicMock()
    fake_resp.content = feed_bytes
    fake_resp.raise_for_status = MagicMock()
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: fake_resp)


class TestPayloadTrim:
    """_do_fetch_vehicle_positions drops empty-string fields from each vehicle
    dict so the JSON payload is smaller (metro_group is "" for ~95% of
    vehicles). Non-empty fields are always present. Numeric zeros (bearing 0,
    speed 0) are NOT empty strings and must survive.

    P1 — this shapes the wire payload every client polls every 2s; a regression
    that re-adds empty strings inflates bandwidth, and one that drops non-empty
    values breaks rendering.
    """

    def test_empty_fields_are_omitted(self, monkeypatch):
        """A bus trip with no headsign/metro_group → those keys absent."""
        import server
        # trip-aaa resolves to a plain BUS line: line set, but headsign/
        # metro_group stay "" and must be trimmed.
        monkeypatch.setattr(server, "_trip_lookup", {"trip-aaa": [0, 0, 700, -1]})
        monkeypatch.setattr(server, "_routes", ["route-aaa"])
        monkeypatch.setattr(server, "_lines", ["3"])
        monkeypatch.setattr(server, "_headsigns", [])
        _patched_fetch(monkeypatch, _feed_with_vehicle())

        result = server._do_fetch_vehicle_positions()
        assert len(result["vehicles"]) == 1
        v = result["vehicles"][0]
        # Non-empty fields present
        assert v["id"] == "bus-1"
        assert v["line"] == "3"
        assert v["transport_mode"] == "BUS"
        # Empty-string fields omitted entirely
        assert "metro_group" not in v
        assert "headsign" not in v

    def test_no_empty_string_values_anywhere(self, monkeypatch):
        """Regression: the trimmed dict must contain no '' value at all."""
        import server
        monkeypatch.setattr(server, "_trip_lookup", {"trip-aaa": [0, 0, 700, -1]})
        monkeypatch.setattr(server, "_routes", ["route-aaa"])
        monkeypatch.setattr(server, "_lines", ["3"])
        monkeypatch.setattr(server, "_headsigns", [])
        _patched_fetch(monkeypatch, _feed_with_vehicle())

        v = server._do_fetch_vehicle_positions()["vehicles"][0]
        assert "" not in v.values(), f"empty string leaked into payload: {v}"

    def test_populated_fields_are_kept(self, monkeypatch):
        """A metro trip with headsign keeps line/headsign/metro_group."""
        import server
        # type 401 = METRO; line "13" is a known metro line → metro_group set.
        monkeypatch.setattr(server, "_trip_lookup", {"trip-aaa": [0, 0, 401, 0]})
        monkeypatch.setattr(server, "_routes", ["route-aaa"])
        monkeypatch.setattr(server, "_lines", ["13"])
        monkeypatch.setattr(server, "_headsigns", ["Norsborg"])
        _patched_fetch(monkeypatch, _feed_with_vehicle())

        v = server._do_fetch_vehicle_positions()["vehicles"][0]
        assert v["line"] == "13"
        assert v["headsign"] == "Norsborg"
        assert v["transport_mode"] == "METRO"
        # 13 is in METRO_GROUPS → metro_group is non-empty and kept
        assert v.get("metro_group", "") == server.METRO_GROUPS["13"]
        assert v["metro_group"] != ""

    def test_numeric_zero_fields_survive_trim(self, monkeypatch):
        """bearing=0 and speed=0 are valid values, not empty strings — keep them.

        Guards against a naive `if v` trim that would drop falsy numbers.
        """
        import server
        monkeypatch.setattr(server, "_trip_lookup", {"trip-aaa": [0, 0, 700, -1]})
        monkeypatch.setattr(server, "_routes", ["route-aaa"])
        monkeypatch.setattr(server, "_lines", ["3"])
        monkeypatch.setattr(server, "_headsigns", [])
        _patched_fetch(monkeypatch, _feed_with_vehicle(bearing=0.0, speed=0.0))

        v = server._do_fetch_vehicle_positions()["vehicles"][0]
        assert v["bearing"] == 0
        assert v["speed"] == 0
        assert "bearing" in v
        assert "speed" in v

    def test_route_id_kept_when_present(self, monkeypatch):
        """route_id comes from the protobuf trip and should not be trimmed."""
        import server
        monkeypatch.setattr(server, "_trip_lookup", {"trip-aaa": [0, 0, 700, -1]})
        monkeypatch.setattr(server, "_routes", ["route-aaa"])
        monkeypatch.setattr(server, "_lines", ["3"])
        monkeypatch.setattr(server, "_headsigns", [])
        _patched_fetch(monkeypatch, _feed_with_vehicle(route_id="route-xyz"))

        v = server._do_fetch_vehicle_positions()["vehicles"][0]
        assert v["route_id"] == "route-xyz"
        assert v["trip_id"] == "trip-aaa"
