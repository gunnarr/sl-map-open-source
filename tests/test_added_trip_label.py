"""Vehicles on an ADDED trip must say so instead of showing a blank destination.

Trafiklab marks extra departures — replacement service, an inserted unit —
with schedule_relationship = ADDED and no trip_id. The destination lives in the
static timetable, and an added trip is by definition not in it, so no headsign
exists anywhere in the data. Measured 2026-09-23: 11 of 826 vehicles.

Before this, the popup rendered "Tunnelbana 19" with nothing where the
destination goes, which reads as a broken page rather than as missing data.
A vehicle at Högdalen prompted the report.

We label it. We deliberately do NOT infer a destination from bearing or
position: a qualified guess presented as fact is worse on a map people trust
than an honest blank.
"""

import json
from unittest.mock import MagicMock

import pytest
from google.transit import gtfs_realtime_pb2

SAMPLE_LOOKUP = {
    "routes": ["route-green"],
    "lines": ["19"],
    "headsigns": ["Hagsätra"],
    "trips": {"trip-scheduled": [0, 0, 401, 0]},
}

SCHEDULED = gtfs_realtime_pb2.TripDescriptor.SCHEDULED
ADDED = gtfs_realtime_pb2.TripDescriptor.ADDED


def _feed(*specs, feed_ts=1700000000):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_ts
    for i, spec in enumerate(specs):
        e = feed.entity.add()
        e.id = f"entity-{i}"
        vp = e.vehicle
        vp.vehicle.id = spec["id"]
        vp.position.latitude = spec.get("lat", 59.2567)
        vp.position.longitude = spec.get("lon", 18.0283)
        vp.position.speed = spec.get("speed", 5.0)
        vp.trip.trip_id = spec.get("trip_id", "")
        vp.trip.route_id = spec.get("route_id", "route-green")
        if "schedule_relationship" in spec:
            vp.trip.schedule_relationship = spec["schedule_relationship"]
        vp.timestamp = feed_ts
    return feed.SerializeToString()


@pytest.fixture()
def loaded(monkeypatch, tmp_path):
    import server

    region_dir = tmp_path / "regions" / "sl"
    region_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "REGION_DIR", region_dir)
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    for name in ("_trip_lookup", "_route_lookup"):
        monkeypatch.setattr(server, name, {})
    for name in ("_routes", "_lines", "_headsigns"):
        monkeypatch.setattr(server, name, [])
    (tmp_path / "trip_lookup.json").write_text(json.dumps(SAMPLE_LOOKUP), encoding="utf-8")
    server._load_trip_lookup()
    monkeypatch.setattr(server, "TRAFIKLAB_KEY", "test-key")

    def _serve(feed_bytes):
        resp = MagicMock()
        resp.content = feed_bytes
        resp.raise_for_status = MagicMock()
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: resp)
        server._cache.pop("vehicles", None)
        server._cache.pop("trip_updates", None)

    server._serve = _serve
    yield server
    server._cache.pop("vehicles", None)
    server._cache.pop("trip_updates", None)


def _one(server):
    return server._do_fetch_vehicle_positions()["vehicles"][0]


class TestServerFlagsAddedTrips:

    def test_added_trip_without_trip_id_is_flagged(self, loaded):
        loaded._serve(_feed({"id": "extra-1", "trip_id": "",
                             "schedule_relationship": ADDED}))
        v = _one(loaded)
        assert v["line"] == "19"
        assert v.get("added") == 1, "ADDED trip must be marked so the client can label it"
        assert v.get("headsign", "") == "", "an added trip has no timetable destination"

    def test_scheduled_trip_is_not_flagged(self, loaded):
        loaded._serve(_feed({"id": "normal-1", "trip_id": "trip-scheduled",
                             "schedule_relationship": SCHEDULED}))
        v = _one(loaded)
        assert v["headsign"] == "Hagsätra"
        assert "added" not in v, "the flag must not ride along on ordinary traffic"

    def test_unmatched_trip_id_is_not_called_an_extra_departure(self, loaded):
        """A trip_id we simply failed to look up is our staleness, not an
        extra departure. Calling it 'extratur' would be a lie about SL."""
        loaded._serve(_feed({"id": "stale-1", "trip_id": "trip-not-in-lookup",
                             "schedule_relationship": SCHEDULED}))
        v = _one(loaded)
        assert v["line"] == "19"
        assert "added" not in v

    def test_flag_survives_a_delta_response(self, loaded):
        """The diff sends an explicit field list; a flag left out of it would
        never reach clients that are polling with ?since=."""
        import server
        assert "added" in server._DELTA_FIELDS


class TestClientLabelsMissingDestination:

    def _map(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return (root / "map.html").read_text(encoding="utf-8")

    def test_added_vehicle_is_labelled_extratur(self):
        assert "extratur" in self._map()

    def test_missing_destination_is_named_rather_than_left_blank(self):
        assert "destination saknas" in self._map()

    def test_label_reaches_the_accessible_name_too(self):
        """A screen reader must not be told less than the popup shows.

        The marker's accessible name is the `title`/`alt` that
        createVehicleMarker receives as `ariaLabel` — there is no aria-label
        attribute. Verified rendering in Chrome 2026-09-23: the marker's title
        reads "Tunnelbana 19 extratur".
        """
        html = self._map()
        aria = html[html.index("const ariaLabel ="):]
        aria = aria[:aria.index("\n")]
        assert "destLabel" in aria
