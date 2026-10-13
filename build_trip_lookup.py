#!/usr/bin/env python3
"""
Build trip_lookup.json from GTFS Regional Static data.

Downloads GTFS zip from Trafiklab for any operator and generates a compact
trip lookup with deduplicated routes, lines, and headsigns.

Usage:
    python3 build_trip_lookup.py                         # default: SL
    python3 build_trip_lookup.py --operator skane         # Skåne
    python3 build_trip_lookup.py --key YOUR_KEY           # explicit key

Without --output/--output-dir the files land in regions/<operator>/, which the
Flask server reads first. (sl-map.gunnar.se builds into the repo root instead,
where its nginx serves them – hence the explicit paths below.)

One download for all three build scripts (Trafiklab's Static quota is small):
    python3 build_trip_lookup.py --save-zip /tmp/sl.zip --output trip_lookup.json
    python3 build_stations.py --gtfs-zip /tmp/sl.zip --output stations.json
    python3 build_geojson.py  --gtfs-zip /tmp/sl.zip --output-dir .
    python3 build_trip_lookup.py --gtfs-zip /tmp/sl.zip   # rebuild, no download
"""

import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict

import requests

GTFS_URL_TEMPLATE = "https://opendata.samtrafiken.se/gtfs/{op}/{op}.zip"


def download_gtfs(key, operator="sl"):
    """Download GTFS zip and return as ZipFile object."""
    url = GTFS_URL_TEMPLATE.format(op=operator)
    print(f"Downloading GTFS Static from {url} ...")
    resp = requests.get(url, params={"key": key}, timeout=120,
                        headers={"Accept-Encoding": "gzip"})
    resp.raise_for_status()
    print(f"  Downloaded {len(resp.content):,} bytes")
    return zipfile.ZipFile(io.BytesIO(resp.content))


def download_gtfs_to_file(key, path, operator="sl"):
    """Download GTFS zip and save the raw bytes to `path`.

    Used when multiple build scripts need the same GTFS Static snapshot in
    one run (e.g. build_stations.py + build_geojson.py back-to-back) — one
    download instead of one per script avoids tripping Trafiklab's rate
    limit on repeated requests.
    """
    url = GTFS_URL_TEMPLATE.format(op=operator)
    print(f"Downloading GTFS Static from {url} ...")
    resp = requests.get(url, params={"key": key}, timeout=120,
                        headers={"Accept-Encoding": "gzip"})
    resp.raise_for_status()
    with open(path, "wb") as f:
        f.write(resp.content)
    print(f"  Downloaded {len(resp.content):,} bytes to {path}")


def read_csv(zf, filename):
    """Read a CSV file from the zip, return list of dicts."""
    with zf.open(filename) as f:
        # Handle BOM
        text = io.TextIOWrapper(f, encoding="utf-8-sig")
        return list(csv.DictReader(text))


def build_lookup(zf):
    """Build the compact trip lookup from GTFS data."""
    # 1. Read routes: route_id -> (route_short_name, route_type)
    routes_data = read_csv(zf, "routes.txt")
    route_map = {}
    for r in routes_data:
        route_map[r["route_id"]] = {
            "line": r.get("route_short_name", ""),
            "type": int(r.get("route_type", 700)),
        }
    print(f"  Routes: {len(route_map)}")

    # 2. Read trips: trip_id -> (route_id, trip_headsign)
    trips_data = read_csv(zf, "trips.txt")
    trip_map = {}
    for t in trips_data:
        trip_map[t["trip_id"]] = {
            "route_id": t["route_id"],
            "headsign": t.get("trip_headsign", ""),
        }
    print(f"  Trips: {len(trip_map)}")

    # 3. For trips without headsign, try stop_times.txt stop_headsign
    #    (GTFS Sweden 3 has headsigns in stop_times, SL Regional may too)
    missing_headsigns = {tid for tid, t in trip_map.items() if not t["headsign"]}
    if missing_headsigns:
        print(f"  {len(missing_headsigns)} trips missing headsign, checking stop_times.txt ...")
        try:
            stop_times = read_csv(zf, "stop_times.txt")
            for st in stop_times:
                tid = st["trip_id"]
                sh = st.get("stop_headsign", "")
                if tid in missing_headsigns and sh:
                    trip_map[tid]["headsign"] = sh
                    missing_headsigns.discard(tid)
            print(f"  After stop_times: {len(missing_headsigns)} still missing headsign")
        except KeyError:
            print("  stop_times.txt not found or missing stop_headsign column")

    # 4. Build deduplicated arrays
    routes_set = {}  # route_id -> index
    lines_set = {}   # line_name -> index
    headsigns_set = {}  # headsign -> index
    trips_out = {}

    for trip_id, trip in trip_map.items():
        route_id = trip["route_id"]
        route_info = route_map.get(route_id)
        if not route_info:
            continue

        line_name = route_info["line"]
        route_type = route_info["type"]
        headsign = trip["headsign"]

        # Skip trips without line name
        if not line_name:
            continue

        # Deduplicate
        if route_id not in routes_set:
            routes_set[route_id] = len(routes_set)
        if line_name not in lines_set:
            lines_set[line_name] = len(lines_set)
        if headsign not in headsigns_set:
            headsigns_set[headsign] = len(headsigns_set)

        trips_out[trip_id] = [
            routes_set[route_id],
            lines_set[line_name],
            route_type,
            headsigns_set[headsign] if headsign else -1,
        ]

    # Build output
    routes_arr = [""] * len(routes_set)
    for k, v in routes_set.items():
        routes_arr[v] = k

    lines_arr = [""] * len(lines_set)
    for k, v in lines_set.items():
        lines_arr[v] = k

    headsigns_arr = [""] * len(headsigns_set)
    for k, v in headsigns_set.items():
        headsigns_arr[v] = k

    result = {
        "routes": routes_arr,
        "lines": lines_arr,
        "headsigns": headsigns_arr,
        "trips": trips_out,
    }

    print(f"\nResult: {len(routes_arr)} routes, {len(lines_arr)} lines, "
          f"{len(headsigns_arr)} headsigns, {len(trips_out)} trips")

    return result


# Per-operator validation thresholds
#
# The absolute floors (min_trips, min_file_size) only catch a genuinely broken
# feed — keep them well below normal, or ordinary day-to-day variation trips
# them. The ratio checks are what guard against a real change in feed shape:
# they compare against the file currently live, so they follow the feed as it
# drifts instead of going stale like a fixed number does.
VALIDATION_DEFAULTS = {
    "min_trips": 1_000,
    "required_lines": [],
    "min_file_size": 100_000,
    "max_size_ratio": 2.0,
    "min_size_ratio": 0.85,
}

VALIDATION_OVERRIDES = {
    "sl": {"min_trips": 50_000, "required_lines": ["80"], "min_file_size": 1_500_000},
}


def _validation_config(operator):
    """Get validation thresholds for an operator."""
    cfg = dict(VALIDATION_DEFAULTS)
    cfg.update(VALIDATION_OVERRIDES.get(operator, {}))
    return cfg


def validate_trip_lookup(new_path, old_path=None, operator="sl"):
    """Validate a trip_lookup.json file. Returns (ok, errors)."""
    cfg = _validation_config(operator)
    errors = []

    try:
        with open(new_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, [f"Cannot read/parse file: {e}"]

    for key in ("routes", "lines", "headsigns", "trips"):
        if key not in data:
            errors.append(f"Missing key: {key}")
    if errors:
        return False, errors

    trip_count = len(data["trips"])
    if trip_count < cfg["min_trips"]:
        errors.append(f"Too few trips: {trip_count:,} (minimum {cfg['min_trips']:,})")

    for line in cfg["required_lines"]:
        if line not in data["lines"]:
            errors.append(f"Required line '{line}' missing")

    file_size = os.path.getsize(new_path)
    if file_size < cfg["min_file_size"]:
        errors.append(f"File too small: {file_size:,} bytes (minimum {cfg['min_file_size']:,})")

    if old_path and os.path.exists(old_path):
        old_size = os.path.getsize(old_path)
        if old_size > 0 and file_size > old_size * cfg["max_size_ratio"]:
            errors.append(
                f"New file too large: {file_size:,} vs old {old_size:,} "
                f"(>{cfg['max_size_ratio']}x)"
            )
        if old_size > 0 and file_size < old_size * cfg["min_size_ratio"]:
            errors.append(
                f"New file too small: {file_size:,} vs old {old_size:,} "
                f"(<{cfg['min_size_ratio']}x)"
            )

    return len(errors) == 0, errors


def _read_key():
    """Read GTFS Static API key from args, env, or .env file."""
    key = os.environ.get("GTFS_STATIC_KEY", "")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__) or ".", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("GTFS_STATIC_KEY="):
                        key = line.strip().split("=", 1)[1].strip().strip("'\"")
    return key


def main():
    parser = argparse.ArgumentParser(description="Build trip_lookup.json from GTFS Static")
    parser.add_argument("--operator", default="sl", help="Trafiklab operator slug (default: sl)")
    parser.add_argument("--key", help="GTFS Regional Static API key")
    parser.add_argument("--output", help="Output file path (default: regions/<operator>/trip_lookup.json)")
    parser.add_argument("--validate", metavar="FILE", help="Validate an existing file and exit")
    parser.add_argument("--old", metavar="FILE", help="Old file for size comparison (used with --validate)")
    zip_source = parser.add_mutually_exclusive_group()
    zip_source.add_argument("--gtfs-zip", metavar="PATH",
                            help="Use an already-downloaded GTFS Static zip (no download, no key needed)")
    zip_source.add_argument("--save-zip", metavar="PATH",
                            help="Keep the downloaded GTFS zip at PATH, for build_stations.py/build_geojson.py --gtfs-zip")
    args = parser.parse_args()

    if args.validate:
        ok, errors = validate_trip_lookup(args.validate, args.old, operator=args.operator)
        if ok:
            with open(args.validate) as f:
                data = json.load(f)
            print(f"OK: {len(data['trips']):,} trips, {len(data['lines']):,} lines, "
                  f"{os.path.getsize(args.validate):,} bytes")
        else:
            for e in errors:
                print(f"FAIL: {e}")
        sys.exit(0 if ok else 1)

    output = args.output or os.path.join("regions", args.operator, "trip_lookup.json")

    if args.gtfs_zip:
        if not os.path.isfile(args.gtfs_zip):
            print(f"ERROR: GTFS zip not found: {args.gtfs_zip}")
            sys.exit(1)
        print(f"Using local GTFS zip: {args.gtfs_zip}")
        zf = zipfile.ZipFile(args.gtfs_zip)
    else:
        key = args.key or _read_key()
        if not key:
            print("ERROR: No API key. Use --key or set GTFS_STATIC_KEY in .env (or pass --gtfs-zip)")
            sys.exit(1)
        if args.save_zip:
            download_gtfs_to_file(key, args.save_zip, operator=args.operator)
            zf = zipfile.ZipFile(args.save_zip)
        else:
            zf = download_gtfs(key, operator=args.operator)

    if os.path.dirname(output):
        os.makedirs(os.path.dirname(output), exist_ok=True)
    print(f"  Files in zip: {zf.namelist()}")
    lookup = build_lookup(zf)

    # Write compact JSON (no indent, sorted keys for reproducibility)
    with open(output, "w") as f:
        json.dump(lookup, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(output)
    print(f"\nWrote {output} ({size:,} bytes)")


if __name__ == "__main__":
    main()
