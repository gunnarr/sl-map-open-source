#!/usr/bin/env python3
"""
Build GeoJSON track files from GTFS Regional Static shapes.txt.

Groups shapes by transport mode and outputs one GeoJSON file per mode.
Applies Douglas-Peucker simplification to reduce file size.

Usage:
    python3 build_geojson.py                         # default: SL
    python3 build_geojson.py --operator skane         # Skåne
"""

import argparse
import json
import math
import os
import sys
import zipfile
from collections import defaultdict

from build_trip_lookup import download_gtfs, read_csv, _read_key

RAIL_ROUTE_TYPES = {
    100: "train", 106: "train",
    401: "metro",
    900: "tram",
}

MODE_FILENAMES = {
    "metro": "metro_lines.geojson",
    "tram": "tram_lines.geojson",
    "train": "train_lines.geojson",
}


def _perpendicular_distance(point, start, end):
    """Distance from point to line segment start-end (in degrees, approximate)."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    t = max(0, min(1, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)))
    proj = (start[0] + t * dx, start[1] + t * dy)
    return math.hypot(point[0] - proj[0], point[1] - proj[1])


def simplify(coords, epsilon=0.0002):
    """Douglas-Peucker simplification."""
    if len(coords) <= 2:
        return coords
    max_dist = 0
    max_idx = 0
    for i in range(1, len(coords) - 1):
        d = _perpendicular_distance(coords[i], coords[0], coords[-1])
        if d > max_dist:
            max_dist = d
            max_idx = i
    if max_dist > epsilon:
        left = simplify(coords[:max_idx + 1], epsilon)
        right = simplify(coords[max_idx:], epsilon)
        return left[:-1] + right
    return [coords[0], coords[-1]]


def build_geojson(zf, metro_groups=None, colors=None, tram_names=None):
    """Build GeoJSON features grouped by mode. Returns dict: mode → GeoJSON FeatureCollection."""
    metro_groups = metro_groups or {}
    colors = colors or {}
    tram_names = tram_names or {}
    metro_colors = colors.get("metro", {})
    # 1. Rail routes
    routes = read_csv(zf, "routes.txt")
    rail_routes = {}
    route_names = {}
    for r in routes:
        rt = int(r.get("route_type", 700))
        mode = RAIL_ROUTE_TYPES.get(rt)
        if mode:
            rail_routes[r["route_id"]] = mode
            route_names[r["route_id"]] = r.get("route_short_name", r.get("route_long_name", ""))
    print(f"  Rail routes: {len(rail_routes)}")

    # 2. Map shape_id → (route_id, mode, line_name) via trips
    trips = read_csv(zf, "trips.txt")
    shape_route = {}  # shape_id → route_id (first seen)
    for t in trips:
        rid = t["route_id"]
        sid = t.get("shape_id", "")
        if rid in rail_routes and sid and sid not in shape_route:
            shape_route[sid] = rid
    print(f"  Rail shapes: {len(shape_route)}")

    # 3. Read shapes.txt
    try:
        shapes_data = read_csv(zf, "shapes.txt")
    except KeyError:
        print("  WARNING: shapes.txt not found — no GeoJSON output")
        return {}

    shape_points = defaultdict(list)
    for s in shapes_data:
        sid = s["shape_id"]
        if sid in shape_route:
            shape_points[sid].append((
                int(s.get("shape_pt_sequence", 0)),
                float(s["shape_pt_lon"]),
                float(s["shape_pt_lat"]),
            ))
    for sid in shape_points:
        shape_points[sid].sort(key=lambda x: x[0])

    # 4. Group by mode + line, pick longest shape per line
    mode_line_shapes = defaultdict(dict)  # mode → {line → [(coords, shape_id), ...]}
    for sid, points in shape_points.items():
        rid = shape_route[sid]
        mode = rail_routes[rid]
        line = route_names.get(rid, "")
        coords = [(p[1], p[2]) for p in points]  # (lon, lat)
        key = f"{line}"
        if key not in mode_line_shapes[mode]:
            mode_line_shapes[mode][key] = []
        mode_line_shapes[mode][key].append((coords, sid, line))

    # 5. Build GeoJSON per mode — pick longest shape per line for each direction
    result = {}
    for mode, line_shapes in mode_line_shapes.items():
        features = []
        for line_key, shapes in sorted(line_shapes.items()):
            # Sort by length descending, take up to 2 (one per direction)
            shapes.sort(key=lambda x: len(x[0]), reverse=True)
            seen_directions = set()
            for coords, sid, line in shapes:
                # Deduplicate on the actual geometry, not on the endpoints.
                # The old key was the first and last point rounded to two
                # decimals — over a kilometre at this latitude — so two
                # genuinely different alignments sharing terminals counted as
                # the same shape. One of them vanished from the map, and
                # snap-to-track then pulled its vehicles onto the wrong rails.
                if len(coords) >= 2:
                    key = tuple((round(lat, 5), round(lon, 5)) for lat, lon in coords)
                    reverse = tuple(reversed(key))
                    if key in seen_directions or reverse in seen_directions:
                        continue
                    seen_directions.add(key)

                if mode == "metro":
                    group = metro_groups.get(line, "")
                    properties = {
                        "name": f"Linje {line}",
                        "ref": line,
                        "group": group,
                        "color": metro_colors.get(group, "#888"),
                    }
                elif mode == "tram":
                    properties = {
                        "name": tram_names.get(line, line),
                        "ref": line,
                        "color": colors.get("tram", "#888"),
                    }
                else:  # train
                    properties = {
                        "name": f"Pendeltåg {line}",
                        "ref": line,
                        "color": colors.get("train", "#888"),
                    }

                simplified = simplify(coords)
                features.append({
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[round(c[0], 6), round(c[1], 6)] for c in simplified],
                    },
                })
                if len(features) > 0 and len(seen_directions) >= 2:
                    break

        if features:
            result[mode] = {
                "type": "FeatureCollection",
                "features": features,
            }
            total_points = sum(len(f["geometry"]["coordinates"]) for f in features)
            print(f"  {mode}: {len(features)} features, {total_points} points")

    return result


def main():
    parser = argparse.ArgumentParser(description="Build GeoJSON track files from GTFS Static")
    parser.add_argument("--operator", default="sl", help="Trafiklab operator slug (default: sl)")
    parser.add_argument("--key", help="GTFS Regional Static API key")
    parser.add_argument("--output-dir", help="Output directory (default: regions/<operator>/)")
    parser.add_argument("--config", help="Region config.json path for metro_groups/colors/tram_names")
    parser.add_argument("--gtfs-zip", help="Path to an already-downloaded GTFS Static zip (skips download)")
    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join("regions", args.operator)
    os.makedirs(output_dir, exist_ok=True)

    metro_groups, colors, tram_names = {}, {}, {}
    config_path = args.config or os.path.join("regions", args.operator, "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f)
        transport = cfg.get("transport", {})
        metro_groups = transport.get("metro_groups", {})
        colors = transport.get("colors", {})
        tram_names = transport.get("tram_names", {})

    if args.gtfs_zip:
        print(f"Using local GTFS zip: {args.gtfs_zip}")
        zf = zipfile.ZipFile(args.gtfs_zip)
    else:
        key = args.key or _read_key()
        if not key:
            print("ERROR: No API key. Use --key or set GTFS_STATIC_KEY in .env")
            sys.exit(1)
        zf = download_gtfs(key, operator=args.operator)

    geojson_by_mode = build_geojson(zf, metro_groups=metro_groups, colors=colors, tram_names=tram_names)

    for mode, data in geojson_by_mode.items():
        filename = MODE_FILENAMES.get(mode, f"{mode}_lines.geojson")
        path = os.path.join(output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        size = os.path.getsize(path)
        print(f"  Wrote {path} ({size:,} bytes)")

    if not geojson_by_mode:
        print("\nNo rail routes found — no GeoJSON files written")
    else:
        print(f"\nDone — {len(geojson_by_mode)} GeoJSON files written to {output_dir}/")


if __name__ == "__main__":
    main()
