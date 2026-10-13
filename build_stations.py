#!/usr/bin/env python3
"""
Build stations.json from GTFS Regional Static data.

Extracts rail/tram/train stations by finding stops served by non-bus routes.

Usage:
    python3 build_stations.py                         # default: SL
    python3 build_stations.py --operator skane         # Skåne
"""

import argparse
import json
import os
import sys
import zipfile
from collections import defaultdict

from build_trip_lookup import download_gtfs, read_csv, _read_key

# GTFS route_type → mode name (only rail modes — bus stops excluded)
RAIL_ROUTE_TYPES = {
    100: "train", 106: "train",
    401: "metro",
    900: "tram",
}


def build_stations(zf, metro_groups=None):
    """Build stations from GTFS data. Returns dict grouped by mode."""
    metro_groups = metro_groups or {}

    # 1. Identify rail routes and their line designations
    routes = read_csv(zf, "routes.txt")
    rail_routes = {}  # route_id → mode
    route_lines = {}  # route_id → line designation
    for r in routes:
        rt = int(r.get("route_type", 700))
        mode = RAIL_ROUTE_TYPES.get(rt)
        if mode:
            rail_routes[r["route_id"]] = mode
            route_lines[r["route_id"]] = r.get("route_short_name", "")
    print(f"  Rail routes: {len(rail_routes)}")

    # 2. Map trip_id → (route_id, mode) for rail trips
    trips = read_csv(zf, "trips.txt")
    rail_trips = {}
    for t in trips:
        rid = t["route_id"]
        if rid in rail_routes:
            rail_trips[t["trip_id"]] = rid

    # 3. Find which stops serve which modes and lines
    stop_modes = defaultdict(set)   # stop_id → {mode, ...}
    stop_lines = defaultdict(set)   # stop_id → {line, ...}
    stop_times = read_csv(zf, "stop_times.txt")
    for st in stop_times:
        rid = rail_trips.get(st["trip_id"])
        if rid:
            sid = st["stop_id"]
            stop_modes[sid].add(rail_routes[rid])
            stop_lines[sid].add(route_lines[rid])
    print(f"  Stops served by rail: {len(stop_modes)}")

    # 4. Read all stops, build parent mapping
    stops = read_csv(zf, "stops.txt")
    stop_data = {}
    parent_map = {}
    for s in stops:
        sid = s["stop_id"]
        stop_data[sid] = {
            "name": s.get("stop_name", ""),
            "lat": float(s.get("stop_lat", 0)),
            "lon": float(s.get("stop_lon", 0)),
        }
        if s.get("parent_station"):
            parent_map[sid] = s["parent_station"]

    # 5. Aggregate to parent station level
    station_modes = defaultdict(set)
    station_lines = defaultdict(set)
    for sid, modes in stop_modes.items():
        parent = parent_map.get(sid, sid)
        station_modes[parent].update(modes)
        station_lines[parent].update(stop_lines[sid])

    # 6. Build output grouped by mode
    result = {}
    seen_names = defaultdict(set)
    for sid, modes in sorted(station_modes.items()):
        info = stop_data.get(sid)
        if not info or not info["lat"]:
            continue
        for mode in sorted(modes):
            if mode not in result:
                result[mode] = []
            name = info["name"]
            if name in seen_names[mode]:
                continue
            seen_names[mode].add(name)
            entry = {"name": name, "lat": info["lat"], "lon": info["lon"]}
            if mode == "metro" and metro_groups:
                for line in station_lines[sid]:
                    group = metro_groups.get(line)
                    if group:
                        entry["group"] = group
                        break
            result[mode].append(entry)

    for mode, stations in result.items():
        print(f"  {mode}: {len(stations)} stations")
    return result


def main():
    parser = argparse.ArgumentParser(description="Build stations.json from GTFS Static")
    parser.add_argument("--operator", default="sl", help="Trafiklab operator slug (default: sl)")
    parser.add_argument("--key", help="GTFS Regional Static API key")
    parser.add_argument("--output", help="Output path (default: regions/<operator>/stations.json)")
    parser.add_argument("--config", help="Region config.json path for metro_groups")
    parser.add_argument("--gtfs-zip", help="Path to an already-downloaded GTFS Static zip (skips download)")
    args = parser.parse_args()

    output = args.output or os.path.join("regions", args.operator, "stations.json")
    if os.path.dirname(output):
        os.makedirs(os.path.dirname(output), exist_ok=True)

    metro_groups = {}
    config_path = args.config or os.path.join("regions", args.operator, "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f)
        metro_groups = cfg.get("transport", {}).get("metro_groups", {})

    if args.gtfs_zip:
        print(f"Using local GTFS zip: {args.gtfs_zip}")
        zf = zipfile.ZipFile(args.gtfs_zip)
    else:
        key = args.key or _read_key()
        if not key:
            print("ERROR: No API key. Use --key or set GTFS_STATIC_KEY in .env")
            sys.exit(1)
        zf = download_gtfs(key, operator=args.operator)

    stations = build_stations(zf, metro_groups=metro_groups)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False)

    size = os.path.getsize(output)
    total = sum(len(v) for v in stations.values())
    print(f"\nWrote {output} ({total} stations, {size:,} bytes)")


if __name__ == "__main__":
    main()
