"""The API contract the native iPhone/iPad/Apple TV apps depend on.

The apps (sl-map-native: SLMapAPI.swift, VehicleFeedResponse.swift,
Vehicle.swift, LineShape.swift, Station.swift) are in the App Store and can't
be updated in lockstep with the server. These tests freeze what they read:

- GET /api/vehicles            {"timestamp": int, "vehicles": [vehicle, ...]}
- GET /api/vehicles?since=ts   {"u": [patch], "a": [vehicle], "r": [id], "ts": int}
                               a patch is {"id", <changed fields>}; "" clears a field
- 502 from /api/vehicles       {"error": str, "vehicles": []}
- vehicle keys                 id, lat, lon (required); bearing, speed, line,
                               headsign, status, timestamp, added, delay,
                               route_id, trip_id, transport_mode, metro_group
- GET /api/{metro,train,tram}-lines   GeoJSON FeatureCollection, feature
                               properties name, ref, color (+ group for metro)
- GET /stations.json           {"metro"|"tram"|"train": [{name, lat, lon, group?}]}

Renaming or removing any of these must fail here first. Adding a field is
fine — the apps ignore unknown keys.

Everything runs the real code paths: a protobuf feed through the parser and
the delta logic, and GTFS Static zips through build_geojson.py and
build_stations.py, served by the Flask routes.
"""

import csv
import io
import json
import time
import zipfile
from unittest.mock import MagicMock

import pytest
import requests
from google.transit import gtfs_realtime_pb2

import build_geojson
import build_stations
import server

# --- What the apps decode (Vehicle.CodingKeys / VehiclePatch) ---------------

VEHICLE_REQUIRED = {"id", "lat", "lon"}
VEHICLE_OPTIONAL = {
    "bearing", "speed", "line", "headsign", "status", "timestamp", "added",
    "delay", "route_id", "trip_id", "transport_mode", "metro_group",
}
VEHICLE_KEYS = VEHICLE_REQUIRED | VEHICLE_OPTIONAL
STATUSES = {"INCOMING_AT", "STOPPED_AT", "IN_TRANSIT_TO"}
MODES = {"BUS", "METRO", "TRAM", "TRAIN", "SHIP", "TAXI", "UNKNOWN"}
METRO_GROUPS = {"green", "red", "blue"}

LOOKUP = {
    "routes": ["route-green", "route-bus"],
    "lines": ["17", "3"],
    "headsigns": ["Åkeshov", "Karolinska"],
    "trips": {
        "trip-green": [0, 0, 401, 0],   # metro 17 → green group
        "trip-bus": [1, 1, 700, 1],
    },
}


def _feed(feed_ts, vehicles):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_ts
    for i, spec in enumerate(vehicles):
        e = feed.entity.add()
        e.id = f"entity-{feed_ts}-{i}"   # changes every fetch, like Trafiklab's
        vp = e.vehicle
        vp.vehicle.id = spec["id"]
        vp.position.latitude = spec.get("lat", 59.33)
        vp.position.longitude = spec.get("lon", 18.07)
        vp.position.bearing = spec.get("bearing", 90.0)
        vp.position.speed = spec.get("speed", 5.0)
        vp.trip.trip_id = spec.get("trip_id", "")
        vp.trip.route_id = spec.get("route_id", "")
        if spec.get("added"):
            vp.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.ADDED
        vp.current_status = spec.get("status", 2)
        vp.timestamp = spec.get("timestamp", feed_ts)
    return feed.SerializeToString()


@pytest.fixture()
def feed(monkeypatch, tmp_path):
    """Serve a given protobuf feed as Trafiklab's VehiclePositions."""
    region_dir = tmp_path / "regions" / "sl"
    region_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "REGION_DIR", region_dir)
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    for name in ("_trip_lookup", "_route_lookup"):
        monkeypatch.setattr(server, name, {})
    for name in ("_routes", "_lines", "_headsigns"):
        monkeypatch.setattr(server, name, [])
    (tmp_path / "trip_lookup.json").write_text(json.dumps(LOOKUP), encoding="utf-8")
    server._load_trip_lookup()

    monkeypatch.setattr(server, "TRAFIKLAB_KEY", "dummy-key")
    monkeypatch.setattr(server, "METRO_GROUPS", {"17": "green"})
    monkeypatch.setattr(server, "_prev_vehicles", {})
    server.limiter.enabled = False

    def serve(feed_bytes, delays=None):
        resp = MagicMock(content=feed_bytes, raise_for_status=MagicMock())
        monkeypatch.setattr(server.requests, "get", lambda *a, **k: resp)
        server._cache.pop("vehicles", None)
        # Delays come from the (cached) TripUpdates feed.
        server._cache["trip_updates"] = {"data": delays or {}, "time": time.time()}

    yield serve
    server._cache.pop("vehicles", None)
    server._cache.pop("trip_updates", None)
    server.limiter.enabled = True


def _full_feed():
    return _feed(1000, [
        # A metro vehicle that carries every optional field.
        {"id": "M1", "trip_id": "trip-green", "added": True, "status": 1},
        {"id": "B1", "trip_id": "trip-bus", "lat": 59.30},
        {"id": "B2", "trip_id": "trip-bus", "lat": 59.31},
    ])


# ---------------------------------------------------------------------------
# Full response
# ---------------------------------------------------------------------------

class TestFullResponse:
    def test_envelope(self, client, feed):
        feed(_full_feed())
        resp = client.get("/api/vehicles")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        body = resp.get_json()
        assert set(body) == {"timestamp", "vehicles"}
        assert body["timestamp"] == 1000 and isinstance(body["timestamp"], int)
        assert isinstance(body["vehicles"], list) and len(body["vehicles"]) == 3

    def test_every_vehicle_has_required_keys_and_only_known_keys(self, client, feed):
        feed(_full_feed(), delays={"trip-green": 240})
        for v in client.get("/api/vehicles").get_json()["vehicles"]:
            assert VEHICLE_REQUIRED <= set(v), v
            assert set(v) <= VEHICLE_KEYS, set(v) - VEHICLE_KEYS

    def test_fully_populated_vehicle_has_exactly_the_contract_keys(self, client, feed):
        """Renaming or dropping any field fails here."""
        feed(_full_feed(), delays={"trip-green": 240})
        m1 = {v["id"]: v for v in client.get("/api/vehicles").get_json()["vehicles"]}["M1"]
        assert set(m1) == VEHICLE_KEYS

    def test_value_types_and_enumerations(self, client, feed):
        feed(_full_feed(), delays={"trip-green": 240})
        vehicles = {v["id"]: v for v in client.get("/api/vehicles").get_json()["vehicles"]}
        m1 = vehicles["M1"]
        assert isinstance(m1["id"], str)
        for key in ("lat", "lon", "bearing", "speed"):
            assert isinstance(m1[key], (int, float)), key
        assert isinstance(m1["timestamp"], int)
        assert m1["status"] == "STOPPED_AT" and m1["status"] in STATUSES
        assert m1["transport_mode"] == "METRO" and m1["transport_mode"] in MODES
        assert m1["metro_group"] == "green" and m1["metro_group"] in METRO_GROUPS
        assert m1["line"] == "17" and m1["headsign"] == "Åkeshov"
        assert m1["route_id"] == "route-green" and m1["trip_id"] == "trip-green"
        assert m1["added"] == 1                      # apps: (added ?? 0) != 0
        assert m1["delay"] == 240 and isinstance(m1["delay"], int)
        assert vehicles["B1"]["transport_mode"] == "BUS"
        assert vehicles["B1"]["status"] in STATUSES

    def test_vehicle_id_is_the_stable_vehicle_id_not_entity_id(self, client, feed):
        feed(_full_feed())
        ids = {v["id"] for v in client.get("/api/vehicles").get_json()["vehicles"]}
        assert ids == {"M1", "B1", "B2"}

    def test_empty_optional_fields_are_omitted_not_null(self, client, feed):
        """Clients treat missing and "" alike; null is never sent."""
        feed(_full_feed())
        b1 = {v["id"]: v for v in client.get("/api/vehicles").get_json()["vehicles"]}["B1"]
        assert "metro_group" not in b1 and "added" not in b1 and "delay" not in b1
        assert None not in b1.values()


# ---------------------------------------------------------------------------
# Delta response (?since=)
# ---------------------------------------------------------------------------

class TestDeltaResponse:
    def _two_versions(self, client, feed):
        feed(_full_feed(), delays={"trip-green": 240})
        assert client.get("/api/vehicles").get_json()["timestamp"] == 1000
        feed(_feed(1002, [
            # M1 moved, lost its delay (→ cleared) and is no longer an extra departure
            {"id": "M1", "trip_id": "trip-green", "lat": 59.34, "status": 1},
            # B1 unchanged → not in u
            {"id": "B1", "trip_id": "trip-bus", "lat": 59.30, "timestamp": 1000},
            # B2 gone → r; N1 new → a
            {"id": "N1", "trip_id": "trip-bus", "lat": 59.40},
        ]))
        return client.get("/api/vehicles?since=1000")

    def test_envelope(self, client, feed):
        resp = self._two_versions(client, feed)
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body) == {"u", "a", "r", "ts"}
        assert body["ts"] == 1002 and isinstance(body["ts"], int)
        assert isinstance(body["u"], list) and isinstance(body["a"], list) and isinstance(body["r"], list)

    def test_updates_carry_id_and_only_changed_fields(self, client, feed):
        body = self._two_versions(client, feed).get_json()
        u = {p["id"]: p for p in body["u"]}
        assert "B1" not in u
        m1 = u["M1"]
        assert m1["lat"] == 59.34
        assert "line" not in m1 and "headsign" not in m1    # unchanged → absent (keep)
        assert set(m1) <= {"id"} | set(server._DELTA_FIELDS)

    def test_empty_string_clears_a_field(self, client, feed):
        m1 = {p["id"]: p for p in self._two_versions(client, feed).get_json()["u"]}["M1"]
        assert m1["delay"] == ""
        assert m1["added"] == ""

    def test_added_and_removed(self, client, feed):
        body = self._two_versions(client, feed).get_json()
        assert [v["id"] for v in body["a"]] == ["N1"]
        assert VEHICLE_REQUIRED <= set(body["a"][0]) and set(body["a"][0]) <= VEHICLE_KEYS
        assert body["r"] == ["B2"] and all(isinstance(x, str) for x in body["r"])

    def test_unknown_base_falls_back_to_a_full_response(self, client, feed):
        feed(_full_feed())
        body = client.get("/api/vehicles?since=1").get_json()
        assert set(body) == {"timestamp", "vehicles"}


def test_delta_fields_cover_every_optional_vehicle_field():
    """A field missing here silently stops updating for delta clients."""
    assert set(server._DELTA_FIELDS) == VEHICLE_KEYS - {"id"}


# ---------------------------------------------------------------------------
# Upstream failure
# ---------------------------------------------------------------------------

def test_upstream_failure_is_502_with_error_and_empty_vehicles(client, feed, monkeypatch):
    feed(b"")

    def down(*a, **k):
        raise requests.ConnectionError("trafiklab down")

    monkeypatch.setattr(server.requests, "get", down)
    server._cache.pop("vehicles", None)
    resp = client.get("/api/vehicles")
    assert resp.status_code == 502
    body = resp.get_json()
    assert set(body) == {"error", "vehicles"}
    assert isinstance(body["error"], str) and body["error"]
    assert body["vehicles"] == []


# ---------------------------------------------------------------------------
# Track geometry and stations (built by the real build scripts)
# ---------------------------------------------------------------------------

def _csv(rows, fields):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return out.getvalue()


def _static_zip():
    routes = [
        {"route_id": "RM", "route_short_name": "17", "route_type": "401"},
        {"route_id": "RT", "route_short_name": "30", "route_type": "900"},
        {"route_id": "RJ", "route_short_name": "41", "route_type": "100"},
    ]
    trips = [{"trip_id": f"T{r['route_id']}", "route_id": r["route_id"], "shape_id": f"S{r['route_id']}"}
             for r in routes]
    shapes, stops, stop_times = [], [], []
    for n, r in enumerate(routes):
        for i in range(3):
            shapes.append({"shape_id": f"S{r['route_id']}", "shape_pt_sequence": i,
                           "shape_pt_lat": 59.30 + n * 0.01 + i * 0.01, "shape_pt_lon": 18.0 + i * 0.01})
        stops.append({"stop_id": f"P{n}", "stop_name": f"Station {n}", "stop_lat": 59.3 + n * 0.01,
                      "stop_lon": 18.0, "parent_station": ""})
        stop_times.append({"trip_id": f"T{r['route_id']}", "stop_id": f"P{n}", "stop_sequence": 1})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("routes.txt", _csv(routes, ["route_id", "route_short_name", "route_type"]))
        zf.writestr("trips.txt", _csv(trips, ["trip_id", "route_id", "shape_id"]))
        zf.writestr("shapes.txt", _csv(shapes, ["shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon"]))
        zf.writestr("stops.txt", _csv(stops, ["stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"]))
        zf.writestr("stop_times.txt", _csv(stop_times, ["trip_id", "stop_id", "stop_sequence"]))
    buf.seek(0)
    return zipfile.ZipFile(buf)


@pytest.fixture()
def built_static(app, monkeypatch):
    """Build the line GeoJSON and stations.json with the real build scripts."""
    region_dir = server.BASE_DIR / "regions" / "sl"
    region_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "REGION_DIR", region_dir)
    monkeypatch.setattr(server, "_geojson_cache", {})
    colors = {"metro": {"green": "#148541"}, "tram": "#1a5276", "train": "#8e44ad"}
    with _static_zip() as zf:
        lines = build_geojson.build_geojson(zf, metro_groups={"17": "green"}, colors=colors,
                                            tram_names={"30": "Tvärbanan"})
    with _static_zip() as zf:
        stations = build_stations.build_stations(zf, metro_groups={"17": "green"})
    for mode, fc in lines.items():
        (region_dir / build_geojson.MODE_FILENAMES[mode]).write_text(json.dumps(fc), encoding="utf-8")
    (region_dir / "stations.json").write_text(json.dumps(stations), encoding="utf-8")


@pytest.mark.parametrize("path,extra", [
    ("/api/metro-lines", {"group"}),
    ("/api/train-lines", set()),
    ("/api/tram-lines", set()),
])
def test_line_geojson_contract(client, built_static, path, extra):
    resp = client.get(path)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["type"] == "FeatureCollection"
    assert body["features"]
    for f in body["features"]:
        props = f["properties"]
        assert {"name", "ref", "color"} | extra <= set(props), props
        assert isinstance(props["ref"], str) and props["ref"]
        assert isinstance(props["name"], str)
        assert props["color"].startswith("#")
        assert f["geometry"]["type"] in ("LineString", "MultiLineString")
        lon, lat = f["geometry"]["coordinates"][0]     # GeoJSON order: [lon, lat]
        assert 17 < lon < 19.5 and 58.5 < lat < 60.5
    if "group" in extra:
        assert body["features"][0]["properties"]["group"] in METRO_GROUPS


def test_stations_json_contract(client, built_static):
    resp = client.get("/stations.json")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) <= {"metro", "tram", "train"}
    assert set(body) == {"metro", "tram", "train"}
    for kind, stations in body.items():
        for s in stations:
            assert {"name", "lat", "lon"} <= set(s), s
            assert isinstance(s["name"], str) and s["name"]
            assert isinstance(s["lat"], float) and isinstance(s["lon"], float)
    assert body["metro"][0]["group"] in METRO_GROUPS
