"""
SL GTFS-RT Vehicle Positions Proxy Server

Proxies real-time vehicle position data from Trafiklab's GTFS-RT feed,
converts protobuf to JSON, and serves it alongside static frontend files.
"""

import json
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, request, send_from_directory
from flask_limiter import Limiter
from google.transit import gtfs_realtime_pb2

load_dotenv()

app = Flask(__name__, static_folder=None)

def _compute_version():
    """Version = newest mtime among files that change user-facing behavior.
    Tracks server.py + map.html (+ site-config.js and the optional
    private_ext.py) so changes to any of them bump the version and trigger
    screensaver auto-reload. All Gunicorn workers see same value."""
    here = os.path.dirname(os.path.abspath(__file__))
    files = [__file__] + [os.path.join(here, f) for f in ("map.html", "site-config.js", "private_ext.py")]
    mtimes = [os.path.getmtime(f) for f in files if os.path.exists(f)]
    return str(int(max(mtimes))) if mtimes else "0"

SERVER_START = _compute_version()


# Reverse proxies whose X-Real-IP header we believe. In production nginx sits on
# the same host and connects from 127.0.0.1. Anyone else could put any IP in the
# header to dodge rate limits or pass the localhost check on /api/config.
TRUSTED_PROXIES = {
    ip.strip() for ip in os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if ip.strip()
}


def _get_real_ip():
    """Client IP: X-Real-IP when the request comes from a trusted proxy."""
    if request.remote_addr in TRUSTED_PROXIES:
        return request.headers.get("X-Real-IP", request.remote_addr)
    return request.remote_addr


limiter = Limiter(
    _get_real_ip,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Region config
# ---------------------------------------------------------------------------
REGION = os.environ.get("REGION", "sl")
REGION_DIR = BASE_DIR / "regions" / REGION



def _deep_merge(base, overlay):
    """Merge overlay into base (nested dicts merged, everything else replaced)."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_region_config(region_dir):
    """config.json, with identity.private.json merged on top when it exists.

    config.json describes the region and the transit data and can be shared.
    Who runs the site — domain, contact addresses, author — goes in the
    optional identity.private.json next to it, so a published config.json
    carries no personal details. The static route never serves *.private.json.
    """
    config = {}
    config_path = region_dir / "config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    identity_path = region_dir / "identity.private.json"
    if identity_path.exists():
        with open(identity_path, "r", encoding="utf-8") as f:
            _deep_merge(config, json.load(f))
    return config


_region_config = _load_region_config(REGION_DIR)


def _rc(*keys, default=None):
    """Look up a nested key path in region config."""
    val = _region_config
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
        if val is None:
            return default
    return val


# ---------------------------------------------------------------------------
# Constants (loaded from region config, with hardcoded fallbacks for tests)
# ---------------------------------------------------------------------------
TRAFIKLAB_KEY = os.environ.get("TRAFIKLAB_KEY", "")

# How long one VehiclePositions fetch is shared between all visitors. Every
# visitor polls every 2 s, but at most one request per this many seconds per
# worker process reaches Trafiklab — so this, not the number of visitors, sets
# the API quota use: 2 s is ~43 000 calls/day with one worker (or a shared
# cache such as an nginx microcache in front). Raise it on a small key.
VEHICLE_CACHE_SECONDS = max(1, int(os.environ.get("VEHICLE_CACHE_SECONDS", "2")))
# Delays only feed the delay badge: refreshed every 20 s, never more often
# than the positions themselves.
TRIP_UPDATES_CACHE_SECONDS = max(20, VEHICLE_CACHE_SECONDS)
# Public host name of this deployment (no scheme). Used for absolute URLs
# (sitemap) and as the allowed CORS origin. SITE_DOMAIN in the environment
# wins over the region's identity.site_domain.
SITE_DOMAIN = os.environ.get("SITE_DOMAIN") or _rc("identity", "site_domain", default="localhost:8080")
LAUNCH_DATE = _rc("identity", "launch_date")
VEHICLE_POSITIONS_URL = _rc("api", "gtfs_rt_url",
    default="https://opendata.samtrafiken.se/gtfs-rt/sl/VehiclePositions.pb")
TRIP_UPDATES_URL = _rc("api", "gtfs_rt_trip_updates_url",
    default=VEHICLE_POSITIONS_URL.replace("VehiclePositions.pb", "TripUpdates.pb"))

# Only surface a delay badge once a vehicle is meaningfully late — most
# delays are noise (median ~30s in a sample fetch, 90th percentile ~5 min).
# 3 min is a judgment call, not a documented SL threshold — nothing found
# on sl.se/trafikforvaltningen.se ties "on time" to a specific number.
DELAY_BADGE_THRESHOLD_SECONDS = 180
# Modes where a delay badge is useful. Trams/ships were excluded: too few
# matched trips (tram) or too few vehicles (ship) to be worth the clutter.
DELAY_BADGE_MODES = {"BUS", "METRO", "TRAIN"}

# ---------------------------------------------------------------------------
# Trip → route lookup from GTFS static data
# ---------------------------------------------------------------------------

_trip_lookup = {}
_routes = []
_lines = []
_headsigns = []
_route_lookup = {}  # route_id -> (lineIdx, type) — fallback when trip_id is missing/unmatched


def _load_trip_lookup():
    """Load trip_lookup.json (compact format with deduplicated routes/lines/headsigns).

    File format: {"routes": [...], "lines": [...], "headsigns": [...],
                  "trips": {trip_id: [routeIdx, lineIdx, type, headsignIdx]}}
    """
    global _trip_lookup, _routes, _lines, _headsigns, _route_lookup
    path = REGION_DIR / "trip_lookup.json"
    if not path.exists():
        path = BASE_DIR / "trip_lookup.json"
    if not path.exists():
        # The file is untracked (regenerated daily by update-trip-lookup.sh);
        # fall back to that script's backup, e.g. right after a fresh deploy.
        bak = BASE_DIR / "trip_lookup.json.bak"
        if bak.exists():
            path = bak
            print("WARNING: trip_lookup.json missing — loading trip_lookup.json.bak")
    if path.exists():
        with open(path, "r") as f:
            raw = json.load(f)
        _routes = raw["routes"]
        _lines = raw["lines"]
        _headsigns = raw.get("headsigns", [])
        _trip_lookup = raw["trips"]
        # Trafiklab's feed sometimes omits trip_id (e.g. unscheduled/replacement
        # service) but still sets route_id — build a coarser fallback so those
        # vehicles still get a line/mode instead of being dropped entirely.
        _route_lookup = {}
        for info in _trip_lookup.values():
            route_id = _routes[info[0]]
            if route_id not in _route_lookup:
                _route_lookup[route_id] = (info[1], info[2])
        print(f"Loaded {len(_trip_lookup)} trip→route mappings ({len(_routes)} routes, {len(_lines)} lines, {len(_headsigns)} headsigns)")
    else:
        print("WARNING: trip_lookup.json not found. Vehicles won't have line info.")


# Route type mapping: GTFS extended types (from region config or defaults)
_rt_cfg = _rc("transport", "route_type_mode") or {}
ROUTE_TYPE_MODE = {int(k): v for k, v in _rt_cfg.items()} if _rt_cfg else {
    100: "TRAIN", 106: "TRAIN", 401: "METRO", 700: "BUS",
    714: "BUS", 900: "TRAM", 1000: "SHIP", 1501: "TAXI",
}

_mg = _rc("transport", "metro_groups")
METRO_GROUPS = _mg if _mg is not None else {
    "10": "blue", "11": "blue",
    "13": "red", "14": "red",
    "17": "green", "18": "green", "19": "green",
}


# ---------------------------------------------------------------------------
# Simple in-memory cache
# ---------------------------------------------------------------------------

_cache = {}
_cache_locks = {}  # per-key locks
_cache_locks_lock = threading.Lock()  # protects _cache_locks dict


def _get_lock(key):
    """Get or create a lock for a specific cache key."""
    lock = _cache_locks.get(key)
    if lock is None:
        with _cache_locks_lock:
            lock = _cache_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                _cache_locks[key] = lock
    return lock


def _get_cached(key, max_age_seconds):
    """Return (data, True) if cache hit, (None, False) if stale/missing."""
    entry = _cache.get(key)
    if entry and (time.time() - entry["time"]) < max_age_seconds:
        return entry["data"], True
    return None, False


def _get_stale(key):
    """Return stale cached data regardless of age, or None."""
    entry = _cache.get(key)
    if entry:
        return entry["data"]
    return None


def _set_cached(key, data):
    _cache[key] = {"data": data, "time": time.time()}


def _fetch_with_lock(key, max_age_seconds, fetch_fn):
    """Fetch with per-key lock and stale-while-revalidate.

    If another thread is already fetching, return stale data immediately.
    If the upstream fetch fails, return stale data instead of raising.
    """
    cached, hit = _get_cached(key, max_age_seconds)
    if hit:
        return cached

    lock = _get_lock(key)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        # Another thread is fetching — serve stale data
        stale = _get_stale(key)
        if stale is not None:
            return stale
        # No stale data available, must wait
        with lock:
            cached, hit = _get_cached(key, max_age_seconds)
            if hit:
                return cached
            data = fetch_fn()
            _set_cached(key, data)
            return data

    try:
        # Double-check after acquiring lock
        cached, hit = _get_cached(key, max_age_seconds)
        if hit:
            return cached
        try:
            data = fetch_fn()
            _set_cached(key, data)
            return data
        except Exception:
            record_error()
            app.logger.warning("Upstream fetch failed for %r", key, exc_info=True)
            # Upstream failed — return stale data if available
            stale = _get_stale(key)
            if stale is not None:
                return stale
            raise
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Error rate tracking
# ---------------------------------------------------------------------------

_error_timestamps = deque()
_error_timestamps_lock = threading.Lock()
ERROR_WINDOW = 300  # 5 minutes
ERROR_THRESHOLD = 10


def record_error():
    """Record an error timestamp for health monitoring."""
    with _error_timestamps_lock:
        _error_timestamps.append(time.time())


def get_error_rate():
    """Return number of errors in the last ERROR_WINDOW seconds."""
    cutoff = time.time() - ERROR_WINDOW
    with _error_timestamps_lock:
        while _error_timestamps and _error_timestamps[0] < cutoff:
            _error_timestamps.popleft()
        return len(_error_timestamps)


# ---------------------------------------------------------------------------
# Vehicle position helpers
# ---------------------------------------------------------------------------

VEHICLE_STOP_STATUS = {
    0: "INCOMING_AT",
    1: "STOPPED_AT",
    2: "IN_TRANSIT_TO",
}


def _do_fetch_trip_updates():
    """Fetch GTFS-RT TripUpdates and return {trip_id: delay_seconds}.

    Delay is taken from each trip's first stop_time_update — the feed only
    includes stops not yet passed, so the first entry is the nearest
    upcoming stop and the best available proxy for "how late is this
    vehicle right now".
    """
    if not TRAFIKLAB_KEY:
        return {}

    resp = requests.get(
        TRIP_UPDATES_URL,
        params={"key": TRAFIKLAB_KEY},
        timeout=10,
    )
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    delays = {}
    for entity in feed.entity:
        stop_time_updates = entity.trip_update.stop_time_update
        if not stop_time_updates:
            continue
        stu = stop_time_updates[0]
        if stu.HasField("arrival"):
            delays[entity.trip_update.trip.trip_id] = stu.arrival.delay
        elif stu.HasField("departure"):
            delays[entity.trip_update.trip.trip_id] = stu.departure.delay

    return delays


_trip_updates_refresh_lock = threading.Lock()


def _refresh_trip_updates_bg():
    """Refresh delays off the request path, one refresher at a time."""
    if not _trip_updates_refresh_lock.acquire(blocking=False):
        return

    def run():
        try:
            _set_cached("trip_updates", _do_fetch_trip_updates())
        except Exception:
            record_error()
            app.logger.warning("Trip updates fetch failed", exc_info=True)
        finally:
            _trip_updates_refresh_lock.release()

    threading.Thread(target=run, daemon=True, name="trip-updates").start()


def _fetch_trip_updates():
    """Return whatever delay data we have; never block positions on it.

    Delays are a nice-to-have for the badge. Fetching them inline meant a slow
    TripUpdates endpoint held a synchronous Gunicorn worker for up to the 10s
    timeout while the vehicle positions it already had sat unsent — and with
    three workers, a few such requests are the whole pool.

    Fresh, stale or nothing, we answer immediately and refresh in the
    background. The only cost is that the first request after a restart shows
    no delay badges until that refresh lands.
    """
    cached, hit = _get_cached("trip_updates", TRIP_UPDATES_CACHE_SECONDS)
    if hit:
        return cached
    _refresh_trip_updates_bg()
    stale = _get_stale("trip_updates")
    return stale if stale is not None else {}


def _do_fetch_vehicle_positions():
    """Fetch GTFS-RT protobuf feed and parse into a dict (no caching)."""
    if not TRAFIKLAB_KEY:
        return {"error": "TRAFIKLAB_KEY not configured", "vehicles": []}

    resp = requests.get(
        VEHICLE_POSITIONS_URL,
        params={"key": TRAFIKLAB_KEY},
        timeout=10,
    )
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    trip_delays = _fetch_trip_updates()

    vehicles = []
    for entity in feed.entity:
        vp = entity.vehicle
        if not vp.position.latitude:
            continue

        trip_id = vp.trip.trip_id
        route_id = vp.trip.route_id

        # Resolve line info from trip lookup
        line_name = ""
        transport_mode = ""
        metro_group = ""
        headsign = ""
        trip_info = _trip_lookup.get(trip_id)
        route_type = None
        if trip_info:
            # Compact format: [routeIdx, lineIdx, typeInt, headsignIdx]
            line_name = _lines[trip_info[1]]
            route_id = route_id or _routes[trip_info[0]]
            route_type = trip_info[2]
            if len(trip_info) > 3 and trip_info[3] >= 0:
                headsign = _headsigns[trip_info[3]]
        elif route_id in _route_lookup:
            # trip_id missing/unmatched (unscheduled or replacement service) —
            # fall back to route_id so the vehicle isn't silently dropped.
            # No trip-specific headsign is available this way.
            line_idx, route_type = _route_lookup[route_id]
            line_name = _lines[line_idx]

        # An ADDED trip is an extra departure inserted outside the published
        # timetable. The destination lives in the static timetable, so for
        # these it does not exist anywhere in the data — not for us, not for
        # anyone. Flag it so the client can say "extratur" instead of leaving
        # a hole where the destination goes. An unmatched trip_id is NOT this:
        # that is our lookup being stale, and calling it an extra departure
        # would put words in SL's mouth.
        is_added = vp.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.ADDED

        if line_name:
            transport_mode = ROUTE_TYPE_MODE.get(route_type, "BUS")
            # Override: if line is a known metro line, classify as metro
            # (but not if GTFS says it's a ship — Waxholmsbolaget shares line numbers)
            if line_name in METRO_GROUPS and transport_mode != "SHIP":
                transport_mode = "METRO"
                metro_group = METRO_GROUPS[line_name]

        if not line_name:
            if not _rc("transport", "show_unknown", default=False):
                continue
            if vp.position.speed <= 0:
                continue
            transport_mode = "UNKNOWN"

        vehicle = {
            "id": vp.vehicle.id or entity.id,
            "lat": round(vp.position.latitude, 6),
            "lon": round(vp.position.longitude, 6),
            "bearing": vp.position.bearing,
            "speed": round(vp.position.speed, 1),
            "route_id": route_id,
            "trip_id": trip_id,
            "line": line_name,
            "transport_mode": transport_mode,
            "metro_group": metro_group,
            "headsign": headsign,
            "status": VEHICLE_STOP_STATUS.get(
                vp.current_status, "IN_TRANSIT_TO"
            ),
            "timestamp": vp.timestamp,
        }
        if is_added:
            vehicle["added"] = 1

        if transport_mode in DELAY_BADGE_MODES:
            delay = trip_delays.get(trip_id)
            if delay is not None and delay >= DELAY_BADGE_THRESHOLD_SECONDS:
                vehicle["delay"] = delay

        # Omit empty-string fields (metro_group is empty for ~95% of vehicles)
        # — clients treat missing and "" the same, and this trims the payload.
        vehicles.append({k: v for k, v in vehicle.items() if v != ""})

    return {
        "timestamp": feed.header.timestamp,
        "vehicles": vehicles,
    }


def _fetch_vehicle_positions():
    """Fetch vehicle positions with cache and lock."""
    return _fetch_with_lock("vehicles", VEHICLE_CACHE_SECONDS, _do_fetch_vehicle_positions)


# Every mutable field a delta may carry. A field missing from this list is
# never sent to a client polling with ?since=, so it silently stops updating
# for everyone but a cold load — add new vehicle fields here too.
_DELTA_FIELDS = (
    "lat", "lon", "bearing", "speed", "timestamp", "status",
    "line", "headsign", "route_id", "trip_id",
    "transport_mode", "metro_group", "delay", "added",
)

# How many feed versions each worker keeps to diff against, ~2s apart.
#
# This was 5 (~10s) and it was too narrow. A client whose `since` predates the
# oldest snapshot finds no base and gets a full snapshot instead. Measured over
# 24 Sep 2026 — 786 717 ?since= requests — full responses had a lag of p50 7s
# and p90 30s; deltas p50 5s and p90 9s. The split sat exactly on the 10s edge,
# and 22.3% of responses were full.
#
# Clients fall behind because map.html drops any response at or behind the
# timestamp it is showing without advancing _lastVehicleTs (see
# tests/test_stale_response_guard.py). That poll is wasted and the lag grows by
# one interval, so a client pinned to its highest seen timestamp discards every
# answer from a worker that is running behind.
#
# 30 covers ~60s, past the measured p90. Measured on the live box: ~0.2 MB per
# snapshot at 1153 vehicles — ~5.9 MB per worker, ~17.7 MB across three.
_SNAPSHOT_HISTORY = 30

# Keep previous snapshot for diff computation
_prev_vehicles = {}  # {timestamp: {id: vehicle_dict}}
_prev_vehicles_lock = threading.Lock()


def _compute_vehicle_diff(since_ts):
    """Compute diff between current and a previous snapshot.

    Returns None if we don't have the requested snapshot (client should do full fetch).
    Returns {u: [...], a: [...], r: [...], ts: int} otherwise.
    """
    current_data = _fetch_vehicle_positions()
    current_ts = current_data.get("timestamp")
    _store_snapshot(current_data)

    # Falling back to the closest older snapshot is deliberate. The 2026-09-21
    # review (finding 1) called it a P1 and we shipped the exact-base version;
    # measured over two full days it moved 1.3% -> 40.8% of responses to a full
    # snapshot, cost ~9.4 GB a day, and bought nothing we could demonstrate:
    #
    #   - A delta against an *older* base is a superset, not a subset. It sends
    #     more fields than needed, all carrying current values. Harmless.
    #   - The one real defect it prevents is a ghost marker: a vehicle that
    #     appeared after the client's base and vanished before now never lands
    #     in `removed`. Sampling 29 feed versions over 62s found zero vehicles
    #     that flicker on and off within the window.
    #   - Answering with a state the client has moved past is caught in the
    #     client, which drops any response at or behind its rendered timestamp.
    #     tests/test_stale_response_guard.py pins that line — it is the only
    #     thing standing between this fallback and visible backwards jumps, so
    #     do not remove it without restoring an exact-base check here.
    #
    # If the ghost rate is ever measured above zero — trafikstart, trafikslut
    # and disruptions were not sampled — the fix is a snapshot store shared
    # across workers, not exact-base matching against a per-worker store.
    with _prev_vehicles_lock:
        prev = _prev_vehicles.get(since_ts)
        if prev is None:
            older = [ts for ts in _prev_vehicles if ts <= since_ts]
            if older:
                prev = _prev_vehicles[max(older)]

    if prev is None:
        return None

    current_map = {v["id"]: v for v in current_data["vehicles"]}
    prev_ids = set(prev.keys())
    curr_ids = set(current_map.keys())

    added = [current_map[vid] for vid in (curr_ids - prev_ids)]
    removed = list(prev_ids - curr_ids)

    updated = []
    for vid in (curr_ids & prev_ids):
        cv = current_map[vid]
        pv = prev[vid]
        if cv != pv:
            delta = {"id": vid}
            # Include all mutable fields — a vehicle can switch trip mid-stream
            # (new line/headsign/route); omitting them leaves delta clients stale.
            for key in _DELTA_FIELDS:
                if cv.get(key) != pv.get(key):
                    # A field can disappear when it becomes empty (omitted from
                    # the payload) — send "" explicitly so clients clear it.
                    delta[key] = cv.get(key, "")
            updated.append(delta)

    return {"u": updated, "a": added, "r": removed, "ts": current_ts}


def _store_snapshot(data):
    """Store current snapshot for future diff computation."""
    ts = data.get("timestamp")
    if not ts:
        return
    vehicle_map = {v["id"]: v for v in data["vehicles"]}
    with _prev_vehicles_lock:
        keys = sorted(_prev_vehicles.keys())
        while len(keys) >= _SNAPSHOT_HISTORY:
            del _prev_vehicles[keys.pop(0)]
        _prev_vehicles[ts] = vehicle_map




# Extensions add their own pages to the sitemap: each provider is a callable
# returning (path, changefreq, priority, lastmod) tuples, listed after the map.
SITEMAP_PROVIDERS = []


def _build_sitemap_xml():
    """Build sitemap.xml: the map itself plus whatever extensions add."""
    base_url = f"https://{SITE_DOMAIN}"

    # (loc, changefreq, priority, lastmod)
    urls = [
        (f"{base_url}/", "daily", "1.0", None),
    ]
    for provider in SITEMAP_PROVIDERS:
        urls.extend((f"{base_url}{path}", freq, prio, lastmod) for path, freq, prio, lastmod in provider())

    items = []
    for loc, freq, prio, lastmod in urls:
        lastmod_xml = f"\n    <lastmod>{escape(lastmod)}</lastmod>" if lastmod else ""
        items.append(
            f"  <url>\n"
            f"    <loc>{escape(loc)}</loc>"
            f"{lastmod_xml}\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{prio}</priority>\n"
            f"  </url>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(items) + "\n"
        "</urlset>\n"
    )


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Browser-side enforcement only: an unknown Origin gets no ACAO header, so the
# browser refuses to expose the response to third-party JS. Same-origin requests
# (own map.html, screensaver WKWebView) send no Origin header and are unaffected.
# Native HTTP clients (iOS app via URLSession, curl, server-side proxies) ignore
# CORS entirely — this is not a server-side access control.



def _build_allowed_origins(site_domain, extra=""):
    """https://<SITE_DOMAIN> plus any comma-separated origins in CORS_ORIGINS."""
    origins = {f"https://{site_domain}"} if site_domain else set()
    origins.update(o.strip().rstrip("/") for o in extra.split(",") if o.strip())
    return origins


_ALLOWED_ORIGINS = _build_allowed_origins(SITE_DOMAIN, os.environ.get("CORS_ORIGINS", ""))


@app.after_request
def _add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin in _ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/vehicles")
def api_vehicles():
    try:
        since = request.args.get("since", type=int)
        if since:
            diff = _compute_vehicle_diff(since)
            if diff is not None:
                return jsonify(diff)
            # Fall through to full response if we don't have that snapshot

        data = _fetch_vehicle_positions()
        _store_snapshot(data)
        return jsonify(data)
    except requests.RequestException as exc:
        record_error()
        app.logger.warning("vehicles fetch failed: %s", exc)
        return jsonify({"error": "Upstream fetch failed", "vehicles": []}), 502


@app.route("/api/visitors")
def api_visitors():
    """Live visitor count for the map footer.

    The core server has no analytics, so the answer is always 0 and map.html
    hides the figure. An extension with an analytics backend replaces this view.
    """
    return jsonify({"visitors": 0}), 200


@app.route("/api/config")
def api_config():
    """Show active region config. Only accessible from localhost."""
    real_ip = _get_real_ip()
    if real_ip not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Forbidden"}), 403
    safe_config = {
        "region": REGION,
        "region_dir": str(REGION_DIR),
        "config_loaded": bool(_region_config),
        "config": _region_config,
        "resolved": {
            "SITE_DOMAIN": SITE_DOMAIN,
            "LAUNCH_DATE": LAUNCH_DATE,
            "VEHICLE_POSITIONS_URL": VEHICLE_POSITIONS_URL,
            "ROUTE_TYPE_MODE": {str(k): v for k, v in ROUTE_TYPE_MODE.items()},
            "METRO_GROUPS": METRO_GROUPS,
        },
    }
    return jsonify(safe_config)


@app.route("/api/version")
def api_version():
    return jsonify({"v": SERVER_START})


# --- worker census -----------------------------------------------------------
#
# On 2026-08-26 a gunicorn worker deadlocked in a futex right after fork, before
# it had loaded the app. It never served a request, never died, and the master
# kept counting it — for 29 days, with nothing to say so. It surfaced only
# because someone counted processes by hand.
#
# Uptime-Kuma polls /health, so a non-200 here is the alarm. nginx does not
# route on /health, so failing it cannot take the site down.

WORKER_MISMATCH_GRACE = 300  # seconds a mismatch may persist before alarming

# Per worker. Gunicorn spawns replacements before retiring the old ones, so the
# count is legitimately high for a few seconds on every reload — and cron
# reloads daily. Alarming on that would teach us to ignore the alarm.
_worker_mismatch_since = None


def _parse_worker_count(args):
    """The configured worker count from a gunicorn command line, or None.

    Read from the master's own argv rather than hardcoded, so it cannot drift
    from the unit file. gunicorn defaults to 1 when the flag is absent, but we
    do not guess on its behalf — an assumption here would alarm on services
    that never set it.
    """
    for i, arg in enumerate(args):
        value = None
        if arg in (b"--workers", b"-w"):
            value = args[i + 1] if i + 1 < len(args) else None
        elif arg.startswith(b"--workers="):
            value = arg.split(b"=", 1)[1]
        if value:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _worker_census():
    """(live, expected) worker counts, or (None, None) where unknowable.

    Linux-only: it reads /proc. Anywhere without it — a dev machine, a
    container without procfs — reports unknown rather than a fault.
    """
    try:
        master = os.getppid()
        with open(f"/proc/{master}/cmdline", "rb") as fh:
            args = fh.read().split(b"\0")
        expected = _parse_worker_count(args)
        if expected is None:
            return None, None
        with open(f"/proc/{master}/task/{master}/children") as fh:
            live = len(fh.read().split())
        return live, expected
    except (OSError, ValueError):
        return None, None


def _check_workers():
    """(check entry, healthy?) for the /health payload."""
    global _worker_mismatch_since

    live, expected = _worker_census()
    if live is None or expected is None:
        return {"status": "unknown"}, True

    if live == expected:
        _worker_mismatch_since = None
        return {"status": "ok", "live": live, "expected": expected}, True

    now = time.time()
    if _worker_mismatch_since is None:
        _worker_mismatch_since = now
    elapsed = int(now - _worker_mismatch_since)

    entry = {
        "status": "degraded" if elapsed >= WORKER_MISMATCH_GRACE else "settling",
        "live": live,
        "expected": expected,
        "mismatch_seconds": elapsed,
    }
    if entry["status"] == "degraded":
        entry["message"] = (
            f"{live} arbetsprocesser lever, {expected} konfigurerade "
            f"(sedan {elapsed}s)"
        )
        return entry, False
    return entry, True


@app.route("/health")
def health():
    checks = {}
    all_ok = True

    # Freshness: is vehicle data recent?
    # Use _fetch_vehicle_positions() instead of reading _cache directly,
    # so this worker gets fresh data even if it hasn't handled a recent
    # /api/vehicles request (each Gunicorn worker has its own cache).
    # seconds — Trafiklab updates every ~2s; a longer shared cache makes
    # served data older by design, so the allowance grows with it (30 s at 2 s).
    FRESHNESS_THRESHOLD = 28 + VEHICLE_CACHE_SECONDS
    try:
        vehicle_data = _fetch_vehicle_positions()
        data_ts = vehicle_data.get("timestamp", 0)
        age_seconds = int(time.time() - data_ts)
        fresh = age_seconds <= FRESHNESS_THRESHOLD
        checks["freshness"] = {
            "status": "ok" if fresh else "stale",
            "age_seconds": age_seconds,
            "threshold_seconds": FRESHNESS_THRESHOLD,
            "vehicle_count": len(vehicle_data.get("vehicles", [])),
        }
        if not fresh:
            checks["freshness"]["message"] = f"Fordonsdata är {age_seconds}s gammalt"
            all_ok = False
    except Exception:
        checks["freshness"] = {
            "status": "stale",
            "message": "Ingen fordonsdata tillgänglig",
        }
        all_ok = False

    # Trip lookup file
    # Check the loaded lookup, not the file on disk — the file is regenerated
    # daily (and may briefly be absent right after a deploy) while workers
    # serve from the copy loaded at startup, possibly via the .bak fallback.
    if _trip_lookup:
        checks["trip_lookup"] = "ok"
    else:
        checks["trip_lookup"] = "missing"
        all_ok = False

    # Worker census — a stuck worker is invisible from the outside.
    checks["workers"], workers_ok = _check_workers()
    if not workers_ok:
        all_ok = False

    # Error rate
    errors = get_error_rate()
    if errors > ERROR_THRESHOLD:
        checks["error_rate"] = {
            "status": "elevated",
            "errors_last_5min": errors,
            "threshold": ERROR_THRESHOLD,
            "message": f"{errors} fel senaste 5 minuterna",
        }
        all_ok = False
    else:
        checks["error_rate"] = {
            "status": "ok",
            "errors_last_5min": errors,
            "threshold": ERROR_THRESHOLD,
        }

    status = "ok" if all_ok else "error"
    uptime = int(time.time() - int(SERVER_START))
    code = 200 if all_ok else 503

    return jsonify({
        "status": status,
        "uptime": uptime,
        "version": SERVER_START,
        "checks": checks,
    }), code


@app.route("/sitemap.xml")
def sitemap_xml():
    try:
        data = _fetch_with_lock("sitemap_xml", 3600, _build_sitemap_xml)
        resp = make_response(data)
        resp.headers["Content-Type"] = "application/xml; charset=utf-8"
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception as exc:
        app.logger.warning("sitemap.xml build failed: %s", exc)
        return "<!-- error -->", 500


_geojson_cache = {}


def _get_geojson(filename):
    """Load GeoJSON file once, cache in memory forever."""
    if filename in _geojson_cache:
        return _geojson_cache[filename]
    path = REGION_DIR / filename
    if not path.exists():
        path = BASE_DIR / filename
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _geojson_cache[filename] = data
    return data


def _geojson_response(filename):
    data = _get_geojson(filename)
    if data is None:
        return jsonify({"error": f"{filename} not found"}), 404
    resp = jsonify(data)
    resp.cache_control.max_age = 3600
    return resp


@app.route("/api/metro-lines")
def api_metro_lines():
    return _geojson_response("metro_lines.geojson")


@app.route("/api/train-lines")
def api_train_lines():
    return _geojson_response("train_lines.geojson")


@app.route("/api/tram-lines")
def api_tram_lines():
    return _geojson_response("tram_lines.geojson")


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

ALLOWED_STATIC_EXT = {".html", ".css", ".js", ".png", ".jpg", ".webp", ".ico", ".json", ".geojson", ".svg", ".m4a", ".mp4", ".gif", ".pkg"}

# .txt is not a static type: BASE_DIR also holds greetings.txt (contact-form
# messages with e-mail addresses). Only these public-by-design files are served,
# mirroring the nginx allowlist in production.
PUBLIC_TEXT_FILE = re.compile(r"(robots|llms)\.txt|[a-f0-9]{32,}\.txt")


def _asset_dir(filename):
    """Where to serve an asset from: the region's copy if it has one.

    The backend already resolves trip_lookup.json this way, but `/` and the
    static route went straight to BASE_DIR — so REGION=skane served the
    Stockholm client and the root station file alongside Skåne's traffic data.
    """
    candidate = REGION_DIR / filename
    if REGION_DIR != BASE_DIR and candidate.is_file():
        return REGION_DIR
    return BASE_DIR


@app.route("/")
def index():
    resp = make_response(send_from_directory(str(_asset_dir("map.html")), "map.html"))
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/<path:filename>")
def static_files(filename):
    # Block dotfiles (e.g. .env) and directory traversal
    if any(part.startswith(".") for part in filename.split("/")):
        return "Not found", 404
    # Block sensitive directories
    if filename.startswith("deploy/"):
        return "Not found", 404
    # Deployment identity (contact addresses etc.) is config, not content
    if filename.endswith(".private.json"):
        return "Not found", 404
    # Only serve allowed file types
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".txt":
        if not PUBLIC_TEXT_FILE.fullmatch(filename):
            return "Not found", 404
    elif ext not in ALLOWED_STATIC_EXT:
        return "Not found", 404
    resp = make_response(send_from_directory(str(_asset_dir(filename)), filename))
    if ext == ".pkg":
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["Content-Disposition"] = f"attachment; filename={os.path.basename(filename)}"
    elif ext in {".geojson", ".json", ".jpg", ".png", ".webp", ".ico", ".svg", ".m4a"}:
        resp.headers["Cache-Control"] = "public, max-age=86400"
    elif ext in {".html", ".js", ".css"}:
        resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Optional private extension
# ---------------------------------------------------------------------------
# A deployment can add its own pages and services (news, contact form,
# newsletter, screensaver, analytics) from private_ext.py. The server works
# without it; only a missing private_ext module is tolerated, never an error
# inside it.

EXTENSIONS = []

try:
    import private_ext
except ModuleNotFoundError as _exc:
    if _exc.name != "private_ext":
        raise
else:
    private_ext.register(app, sys.modules[__name__])
    EXTENSIONS.append(private_ext)


_load_trip_lookup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # Local development only (production runs gunicorn). Loopback by default:
    # the dev server, and with FLASK_DEBUG=1 its debugger, must not face the LAN
    # unless asked for with HOST=0.0.0.0 (e.g. to test from a phone).
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"Starting {_rc('branding', 'app_name', default='SL Live Map')} (region={REGION}) on http://localhost:{port}")
    if not TRAFIKLAB_KEY:
        print("WARNING: TRAFIKLAB_KEY is not set. /api/vehicles will not work.")
    app.run(host=host, port=port, debug=debug)
