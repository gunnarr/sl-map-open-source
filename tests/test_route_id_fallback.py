"""Regression tests for the route_id fallback in vehicle-position parsing.

P0 — Trafiklab's GTFS-RT feed sometimes sends a vehicle position with an
empty or unmatched `trip_id` (unscheduled service, replacement service) but a
perfectly valid `route_id`. Before the fix, `_do_fetch_vehicle_positions()`
resolved line/mode *only* via `_trip_lookup.get(trip_id)`, so those vehicles
got no line name and were silently `continue`d out of the response — they
vanished from the map. Confirmed against production 2026-09-02: at one point
~35% of active Green Line (17/18/19) metro vehicles were hidden this way.

The fix builds `_route_lookup` (route_id -> (lineIdx, type)) once in
`_load_trip_lookup()` from the same data, and falls back to it when the
trip_id misses. No headsign is available that way (it is trip-specific), so
`headsign` stays empty for such vehicles.

These tests exercise the real protobuf parse path (same approach as
`TestPayloadTrim` in test_vehicle_diff.py) rather than mocking it, so a
regression in the parsing loop actually fails them.
"""

import json
from unittest.mock import MagicMock

import pytest

from google.transit import gtfs_realtime_pb2


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Compact-format lookup covering the shapes we care about:
#   - route-green shared by two trips (dedup / first-wins in _route_lookup)
#   - route-bus  a plain bus route
#   - route-ship a ship route whose *line number* collides with a metro line
SAMPLE_LOOKUP = {
    "routes": ["route-green", "route-bus", "route-ship"],
    "lines": ["17", "3", "10"],
    "headsigns": ["Åkeshov", "Karolinska"],
    "trips": {
        "trip-green-1": [0, 0, 401, 0],   # route-green, line "17", METRO
        "trip-green-2": [0, 0, 401, 1],   # same route, different headsign
        "trip-bus-1": [1, 1, 700, -1],    # route-bus, line "3", BUS
        "trip-ship-1": [2, 2, 1000, -1],  # route-ship, line "10", SHIP
    },
}


def _feed(*vehicles, feed_ts=1700000000):
    """Build a serialized GTFS-RT FeedMessage from vehicle spec dicts.

    Each spec: {id, trip_id, route_id, lat, lon, bearing, speed, timestamp}.
    Multi-vehicle (unlike the single-vehicle helper in test_vehicle_diff.py)
    because the "kept vs dropped" assertions need both cases in one feed.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_ts
    for i, spec in enumerate(vehicles):
        e = feed.entity.add()
        e.id = spec.get("entity_id", f"entity-{i}")
        vp = e.vehicle
        vp.vehicle.id = spec["id"]
        vp.position.latitude = spec.get("lat", 59.33)
        vp.position.longitude = spec.get("lon", 18.07)
        vp.position.bearing = spec.get("bearing", 90.0)
        vp.position.speed = spec.get("speed", 5.0)
        vp.trip.trip_id = spec.get("trip_id", "")
        vp.trip.route_id = spec.get("route_id", "")
        vp.timestamp = spec.get("timestamp", feed_ts)
    return feed.SerializeToString()


@pytest.fixture()
def loaded_lookup(monkeypatch, tmp_path):
    """Load SAMPLE_LOOKUP through the real _load_trip_lookup().

    Deliberately goes through the loader instead of setting the globals by
    hand: _route_lookup is *derived* there, so this is what proves the
    derivation runs. REGION_DIR/BASE_DIR are pointed at empty tmp dirs so the
    real repo copies can never leak in.
    """
    import server

    region_dir = tmp_path / "regions" / "sl"
    region_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "REGION_DIR", region_dir)
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    # monkeypatch restores these after the test, so other modules see the
    # globals they expect.
    monkeypatch.setattr(server, "_trip_lookup", {})
    monkeypatch.setattr(server, "_routes", [])
    monkeypatch.setattr(server, "_lines", [])
    monkeypatch.setattr(server, "_headsigns", [])
    monkeypatch.setattr(server, "_route_lookup", {})

    (tmp_path / "trip_lookup.json").write_text(
        json.dumps(SAMPLE_LOOKUP), encoding="utf-8"
    )
    server._load_trip_lookup()
    return server


@pytest.fixture()
def upstream(monkeypatch):
    """Return a callable that points the upstream feed at given bytes."""
    import server

    monkeypatch.setattr(server, "TRAFIKLAB_KEY", "test-key")

    def _serve(feed_bytes):
        fake_resp = MagicMock()
        fake_resp.content = feed_bytes
        fake_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: fake_resp)
        # The endpoint path is cached (2s / 20s); clear so our feed is parsed.
        server._cache.pop("vehicles", None)
        server._cache.pop("trip_updates", None)

    yield _serve

    server._cache.pop("vehicles", None)
    server._cache.pop("trip_updates", None)


def _by_id(result):
    return {v["id"]: v for v in result["vehicles"]}


# ---------------------------------------------------------------------------
# P0: _route_lookup is derived correctly at load time
# ---------------------------------------------------------------------------

class TestRouteLookupBuild:
    """_load_trip_lookup() must derive route_id -> (lineIdx, type)."""

    def test_route_lookup_populated_from_trips(self, loaded_lookup):
        server = loaded_lookup
        assert server._route_lookup == {
            "route-green": (0, 401),
            "route-bus": (1, 700),
            "route-ship": (2, 1000),
        }

    def test_shared_route_collapses_to_one_entry(self, loaded_lookup):
        """Two trips share route-green (differing only in headsign) and must
        collapse to a single, correct entry.

        Note: the loader's `if route_id not in _route_lookup` guard is not
        observable behaviour — trips on the same GTFS route always carry the
        same line and route_type, so first-wins and last-wins agree. It is an
        idempotency guard, and this test asserts the resulting mapping rather
        than pretending to pin down which write won.
        """
        server = loaded_lookup
        line_idx, route_type = server._route_lookup["route-green"]
        assert server._lines[line_idx] == "17"
        assert route_type == 401

    def test_reload_replaces_rather_than_accumulates(self, loaded_lookup, tmp_path):
        """A second load must not leave stale routes behind (daily cron
        reloads are not a thing today, but the dict is rebuilt, not mutated)."""
        server = loaded_lookup
        other = {
            "routes": ["route-new"],
            "lines": ["42"],
            "headsigns": [],
            "trips": {"trip-new": [0, 0, 700, -1]},
        }
        (tmp_path / "trip_lookup.json").write_text(
            json.dumps(other), encoding="utf-8"
        )
        server._load_trip_lookup()

        assert server._route_lookup == {"route-new": (0, 700)}
        assert "route-green" not in server._route_lookup

    def test_missing_file_leaves_lookup_usable(self, monkeypatch, tmp_path):
        """No trip_lookup.json anywhere: _route_lookup must stay a dict so the
        `route_id in _route_lookup` test in the parse loop can't explode."""
        import server

        region_dir = tmp_path / "regions" / "sl"
        region_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(server, "REGION_DIR", region_dir)
        monkeypatch.setattr(server, "BASE_DIR", tmp_path)
        monkeypatch.setattr(server, "_route_lookup", {})

        server._load_trip_lookup()  # must not raise

        assert isinstance(server._route_lookup, dict)


# ---------------------------------------------------------------------------
# P0: the bug itself — vehicles with no usable trip_id are no longer dropped
# ---------------------------------------------------------------------------

class TestRouteIdFallbackKeepsVehicles:
    """The production bug: valid route_id + empty/unmatched trip_id was
    dropped entirely. It must now resolve line and mode via route_id."""

    def test_empty_trip_id_resolves_via_route_id(self, loaded_lookup, upstream):
        """Green Line metro with trip_id='' — the exact production case."""
        server = loaded_lookup
        upstream(_feed({"id": "metro-1", "trip_id": "", "route_id": "route-green"}))

        v = _by_id(server._do_fetch_vehicle_positions())["metro-1"]
        assert v["line"] == "17"
        assert v["transport_mode"] == "METRO"
        assert v["metro_group"] == "green"
        assert v["route_id"] == "route-green"

    def test_unmatched_trip_id_resolves_via_route_id(self, loaded_lookup, upstream):
        """A non-empty trip_id that simply isn't in the lookup (replacement
        service scheduled after the nightly trip_lookup build)."""
        server = loaded_lookup
        upstream(_feed({
            "id": "metro-2", "trip_id": "trip-not-in-lookup",
            "route_id": "route-green",
        }))

        v = _by_id(server._do_fetch_vehicle_positions())["metro-2"]
        assert v["line"] == "17"
        assert v["transport_mode"] == "METRO"
        assert v["metro_group"] == "green"

    def test_fallback_vehicle_has_no_headsign(self, loaded_lookup, upstream):
        """Headsign is trip-specific — unavailable via route_id. It must be
        empty (and therefore trimmed out of the payload), never borrowed from
        some arbitrary trip on the same route."""
        server = loaded_lookup
        upstream(_feed({"id": "metro-3", "trip_id": "", "route_id": "route-green"}))

        v = _by_id(server._do_fetch_vehicle_positions())["metro-3"]
        assert v.get("headsign", "") == ""
        assert "headsign" not in v  # empty fields are trimmed from the payload

    def test_fallback_for_plain_bus_sets_no_metro_group(self, loaded_lookup, upstream):
        """Non-metro fallback: BUS mode, no metro_group."""
        server = loaded_lookup
        upstream(_feed({"id": "bus-1", "trip_id": "", "route_id": "route-bus"}))

        v = _by_id(server._do_fetch_vehicle_positions())["bus-1"]
        assert v["line"] == "3"
        assert v["transport_mode"] == "BUS"
        assert v.get("metro_group", "") == ""

    def test_fallback_respects_ship_metro_number_collision(self, loaded_lookup, upstream):
        """Waxholmsbolaget line '10' collides with metro line 10. The METRO
        override must stay suppressed for ships on the fallback path too."""
        server = loaded_lookup
        upstream(_feed({"id": "ship-1", "trip_id": "", "route_id": "route-ship"}))

        v = _by_id(server._do_fetch_vehicle_positions())["ship-1"]
        assert v["line"] == "10"
        assert v["transport_mode"] == "SHIP"
        assert v.get("metro_group", "") == ""

    def test_visible_through_the_api_endpoint(self, loaded_lookup, upstream, client):
        """End-to-end: the vehicle actually reaches the map via /api/vehicles."""
        server = loaded_lookup
        upstream(_feed({"id": "metro-4", "trip_id": "", "route_id": "route-green"}))

        resp = client.get("/api/vehicles")

        assert resp.status_code == 200
        vehicles = {v["id"]: v for v in resp.get_json()["vehicles"]}
        assert "metro-4" in vehicles, "route_id-only vehicle missing from /api/vehicles"
        assert vehicles["metro-4"]["line"] == "17"
        assert vehicles["metro-4"]["transport_mode"] == "METRO"
        assert vehicles["metro-4"]["metro_group"] == "green"


# ---------------------------------------------------------------------------
# P0: existing behaviour preserved — unresolvable vehicles are still dropped
# ---------------------------------------------------------------------------

class TestUnresolvableStillDropped:
    """The fallback must not turn into "show everything". A vehicle with
    neither a matching trip_id nor a known route_id has no line info at all
    and stays hidden (show_unknown is off by default)."""

    def test_unknown_trip_and_unknown_route_is_dropped(self, loaded_lookup, upstream):
        server = loaded_lookup
        upstream(_feed({
            "id": "ghost-1", "trip_id": "trip-nope", "route_id": "route-nope",
        }))

        result = server._do_fetch_vehicle_positions()
        assert result["vehicles"] == []

    def test_empty_trip_and_empty_route_is_dropped(self, loaded_lookup, upstream):
        server = loaded_lookup
        upstream(_feed({"id": "ghost-2", "trip_id": "", "route_id": ""}))

        assert server._do_fetch_vehicle_positions()["vehicles"] == []

    def test_resolvable_and_unresolvable_in_one_feed(self, loaded_lookup, upstream):
        """The realistic mix: one keeper, one ghost. Exactly one survives."""
        server = loaded_lookup
        upstream(_feed(
            {"id": "metro-5", "trip_id": "", "route_id": "route-green"},
            {"id": "ghost-3", "trip_id": "", "route_id": "route-nope"},
        ))

        ids = set(_by_id(server._do_fetch_vehicle_positions()))
        assert ids == {"metro-5"}

    def test_dropped_through_the_api_endpoint(self, loaded_lookup, upstream, client):
        server = loaded_lookup
        upstream(_feed({"id": "ghost-4", "trip_id": "", "route_id": "route-nope"}))

        resp = client.get("/api/vehicles")

        assert resp.get_json()["vehicles"] == []


# ---------------------------------------------------------------------------
# P1: the trip_id path is unchanged (the fallback must not shadow it)
# ---------------------------------------------------------------------------

class TestTripLookupStillWins:
    """A matching trip_id must still take precedence — it carries the
    headsign, which the route fallback cannot provide."""

    def test_matching_trip_id_keeps_headsign(self, loaded_lookup, upstream):
        server = loaded_lookup
        upstream(_feed({
            "id": "metro-6", "trip_id": "trip-green-2", "route_id": "route-green",
        }))

        v = _by_id(server._do_fetch_vehicle_positions())["metro-6"]
        assert v["line"] == "17"
        assert v["transport_mode"] == "METRO"
        assert v["metro_group"] == "green"
        assert v["headsign"] == "Karolinska"  # trip-specific, not route-derived

    def test_matching_trip_id_backfills_missing_route_id(self, loaded_lookup, upstream):
        """Pre-existing behaviour: a matched trip supplies route_id when the
        feed omits it."""
        server = loaded_lookup
        upstream(_feed({"id": "bus-2", "trip_id": "trip-bus-1", "route_id": ""}))

        v = _by_id(server._do_fetch_vehicle_positions())["bus-2"]
        assert v["route_id"] == "route-bus"
        assert v["line"] == "3"
        assert v["transport_mode"] == "BUS"
