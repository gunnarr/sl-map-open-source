"""Tests for build_geojson.py — GTFS Static shapes → track-line GeoJSON.

P1 — These files (metro_lines.geojson, tram_lines.geojson, train_lines.geojson)
are what draws the coloured rail tracks on the map. map.html reads
`feature.properties.color` (falling back to grey #888) and, for metro,
`feature.properties.group` to bucket layers per branch.

A regression once shipped where build_geojson() wrote only
`{"properties": {"name": line}}` — no ref/color/group — so every rail line
rendered grey. It went unnoticed for 5.5 months because the file simply was
not rebuilt. These tests lock the property contract so that cannot repeat.
"""

import csv
import io
import json
import os
import zipfile
from unittest.mock import patch

import pytest

import build_geojson as bg


# ---------------------------------------------------------------------------
# Helpers: build synthetic GTFS zips (in memory or on disk)
# ---------------------------------------------------------------------------

ROUTE_FIELDS = ["route_id", "route_short_name", "route_type"]
TRIP_FIELDS = ["trip_id", "route_id", "shape_id"]
SHAPE_FIELDS = ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"]


def _write_csv(zf, name, fieldnames, rows):
    """Write a list of dicts as a CSV member of an open zip."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    zf.writestr(name, buf.getvalue())


def _gtfs_bytes(routes_rows, trips_rows, shapes_rows=None):
    """Return the raw bytes of a GTFS zip. shapes_rows=None omits shapes.txt."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        _write_csv(zf, "routes.txt", ROUTE_FIELDS, routes_rows)
        _write_csv(zf, "trips.txt", TRIP_FIELDS, trips_rows)
        if shapes_rows is not None:
            _write_csv(zf, "shapes.txt", SHAPE_FIELDS, shapes_rows)
    return buf.getvalue()


def _gtfs_zip(routes_rows, trips_rows, shapes_rows=None):
    """Return an in-memory ZipFile opened for reading."""
    return zipfile.ZipFile(io.BytesIO(_gtfs_bytes(routes_rows, trips_rows, shapes_rows)), "r")


def _gtfs_zip_file(path, routes_rows, trips_rows, shapes_rows=None):
    """Write a GTFS zip to disk (for the --gtfs-zip flag). Returns the path."""
    with open(path, "wb") as f:
        f.write(_gtfs_bytes(routes_rows, trips_rows, shapes_rows))
    return str(path)


def _shape(shape_id, points):
    """Build shapes.txt rows from [(lat, lon), ...] in sequence order."""
    return [
        {
            "shape_id": shape_id,
            "shape_pt_lat": f"{lat}",
            "shape_pt_lon": f"{lon}",
            "shape_pt_sequence": str(i),
        }
        for i, (lat, lon) in enumerate(points)
    ]


# A short, slightly bent line so Douglas-Peucker keeps at least the endpoints.
METRO_POINTS = [(59.3000, 18.0000), (59.3200, 18.0500), (59.3400, 18.1500)]
TRAM_POINTS = [(59.3300, 18.0800), (59.3400, 18.1200), (59.3500, 18.2000)]
TRAIN_POINTS = [(59.2000, 17.9000), (59.2500, 17.9500), (59.3000, 18.0000)]


# Realistic slices of regions/sl/config.json → transport
SL_METRO_GROUPS = {"10": "blue", "11": "blue", "13": "red", "14": "red", "17": "green"}
SL_COLORS = {
    "metro": {"green": "#148541", "red": "#d71d24", "blue": "#007db8"},
    "tram": "#1a5276",
    "train": "#8e44ad",
}
SL_TRAM_NAMES = {"7": "Spårväg City", "30": "Tvärbanan"}


def _metro_zip(line="10"):
    return _gtfs_zip(
        routes_rows=[{"route_id": "M1", "route_short_name": line, "route_type": "401"}],
        trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"}],
        shapes_rows=_shape("S1", METRO_POINTS),
    )


# ---------------------------------------------------------------------------
# Metro properties — the regression that shipped grey lines
# ---------------------------------------------------------------------------

class TestMetroProperties:
    """Metro features must carry name, ref, group and colour."""

    def test_metro_feature_has_all_frontend_properties(self):
        """map.html reads ref, group and color — all four keys must exist."""
        result = bg.build_geojson(
            _metro_zip("10"),
            metro_groups=SL_METRO_GROUPS,
            colors=SL_COLORS,
        )

        props = result["metro"]["features"][0]["properties"]
        assert set(props) == {"name", "ref", "group", "color"}

    def test_metro_colour_comes_from_group(self):
        """Line 10 is in the blue group, so it gets the blue metro colour."""
        result = bg.build_geojson(
            _metro_zip("10"),
            metro_groups=SL_METRO_GROUPS,
            colors=SL_COLORS,
        )

        props = result["metro"]["features"][0]["properties"]
        assert props["ref"] == "10"
        assert props["group"] == "blue"
        assert props["color"] == "#007db8"
        assert props["name"] == "Linje 10"

    @pytest.mark.parametrize("line,group,color", [
        ("10", "blue", "#007db8"),
        ("11", "blue", "#007db8"),
        ("13", "red", "#d71d24"),
        ("14", "red", "#d71d24"),
        ("17", "green", "#148541"),
    ])
    def test_each_metro_line_gets_its_branch_colour(self, line, group, color):
        """Every SL metro line maps to its branch group and colour."""
        result = bg.build_geojson(
            _metro_zip(line),
            metro_groups=SL_METRO_GROUPS,
            colors=SL_COLORS,
        )

        props = result["metro"]["features"][0]["properties"]
        assert (props["group"], props["color"]) == (group, color)

    def test_never_writes_bare_name_only_properties(self):
        """Regression guard: properties must not collapse back to {"name": ...}."""
        result = bg.build_geojson(
            _metro_zip("10"),
            metro_groups=SL_METRO_GROUPS,
            colors=SL_COLORS,
        )

        for feature in result["metro"]["features"]:
            assert feature["properties"].keys() != {"name"}
            assert feature["properties"]["color"] != "#888"


# ---------------------------------------------------------------------------
# Metro fallbacks
# ---------------------------------------------------------------------------

class TestMetroFallbacks:
    """Unknown lines and missing config must degrade to grey, not crash."""

    def test_unknown_line_falls_back_to_grey(self):
        """A metro line missing from metro_groups gets empty group and #888."""
        result = bg.build_geojson(
            _metro_zip("99"),
            metro_groups=SL_METRO_GROUPS,
            colors=SL_COLORS,
        )

        props = result["metro"]["features"][0]["properties"]
        assert props["ref"] == "99"
        assert props["group"] == ""
        assert props["color"] == "#888"

    def test_group_without_colour_entry_falls_back_to_grey(self):
        """A known group that has no colour defined still yields #888."""
        result = bg.build_geojson(
            _metro_zip("10"),
            metro_groups={"10": "purple"},
            colors={"metro": {"blue": "#007db8"}},
        )

        props = result["metro"]["features"][0]["properties"]
        assert props["group"] == "purple"
        assert props["color"] == "#888"

    def test_no_config_at_all_still_produces_full_property_set(self):
        """Called with no dicts (defaults None), keys still exist — grey values."""
        result = bg.build_geojson(_metro_zip("10"))

        props = result["metro"]["features"][0]["properties"]
        assert set(props) == {"name", "ref", "group", "color"}
        assert props["color"] == "#888"
        assert props["group"] == ""


# ---------------------------------------------------------------------------
# Tram and train properties
# ---------------------------------------------------------------------------

class TestTramProperties:
    """Tram features use the friendly line name from tram_names."""

    def test_tram_uses_named_line_and_tram_colour(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "TR1", "route_short_name": "30", "route_type": "900"}],
            trips_rows=[{"trip_id": "T1", "route_id": "TR1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", TRAM_POINTS),
        )

        props = bg.build_geojson(
            zf, colors=SL_COLORS, tram_names=SL_TRAM_NAMES
        )["tram"]["features"][0]["properties"]

        assert set(props) == {"name", "ref", "color"}
        assert props["name"] == "Tvärbanan"
        assert props["ref"] == "30"
        assert props["color"] == "#1a5276"

    def test_tram_without_friendly_name_falls_back_to_line_number(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "TR1", "route_short_name": "22", "route_type": "900"}],
            trips_rows=[{"trip_id": "T1", "route_id": "TR1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", TRAM_POINTS),
        )

        props = bg.build_geojson(
            zf, colors=SL_COLORS, tram_names=SL_TRAM_NAMES
        )["tram"]["features"][0]["properties"]

        assert props["name"] == "22"
        assert props["color"] == "#1a5276"

    def test_tram_colour_falls_back_to_grey_without_config(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "TR1", "route_short_name": "30", "route_type": "900"}],
            trips_rows=[{"trip_id": "T1", "route_id": "TR1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", TRAM_POINTS),
        )

        props = bg.build_geojson(zf)["tram"]["features"][0]["properties"]
        assert props["color"] == "#888"


class TestTrainProperties:
    """Train (pendeltåg) features get a Swedish name and the train colour."""

    @pytest.mark.parametrize("route_type", ["100", "106"])
    def test_train_route_types_produce_train_features(self, route_type):
        """Both GTFS train route types (100, 106) map to the train mode."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "P1", "route_short_name": "41", "route_type": route_type}],
            trips_rows=[{"trip_id": "T1", "route_id": "P1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", TRAIN_POINTS),
        )

        props = bg.build_geojson(zf, colors=SL_COLORS)["train"]["features"][0]["properties"]

        assert set(props) == {"name", "ref", "color"}
        assert props["name"] == "Pendeltåg 41"
        assert props["ref"] == "41"
        assert props["color"] == "#8e44ad"

    def test_train_colour_falls_back_to_grey_without_config(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "P1", "route_short_name": "41", "route_type": "100"}],
            trips_rows=[{"trip_id": "T1", "route_id": "P1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", TRAIN_POINTS),
        )

        props = bg.build_geojson(zf)["train"]["features"][0]["properties"]
        assert props["color"] == "#888"


# ---------------------------------------------------------------------------
# Mode selection and geometry
# ---------------------------------------------------------------------------

class TestModeSelection:
    """Only rail modes are exported; buses and ships are ignored."""

    def test_bus_and_ship_routes_are_excluded(self):
        zf = _gtfs_zip(
            routes_rows=[
                {"route_id": "B1", "route_short_name": "4", "route_type": "700"},
                {"route_id": "F1", "route_short_name": "80", "route_type": "1000"},
                {"route_id": "M1", "route_short_name": "10", "route_type": "401"},
            ],
            trips_rows=[
                {"trip_id": "TB", "route_id": "B1", "shape_id": "SB"},
                {"trip_id": "TF", "route_id": "F1", "shape_id": "SF"},
                {"trip_id": "TM", "route_id": "M1", "shape_id": "SM"},
            ],
            shapes_rows=(
                _shape("SB", METRO_POINTS)
                + _shape("SF", TRAM_POINTS)
                + _shape("SM", METRO_POINTS)
            ),
        )

        result = bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS)

        assert set(result) == {"metro"}

    def test_all_three_rail_modes_in_one_run(self):
        """A realistic mixed feed yields one FeatureCollection per rail mode."""
        zf = _gtfs_zip(
            routes_rows=[
                {"route_id": "M1", "route_short_name": "10", "route_type": "401"},
                {"route_id": "TR1", "route_short_name": "30", "route_type": "900"},
                {"route_id": "P1", "route_short_name": "41", "route_type": "100"},
            ],
            trips_rows=[
                {"trip_id": "TM", "route_id": "M1", "shape_id": "SM"},
                {"trip_id": "TT", "route_id": "TR1", "shape_id": "ST"},
                {"trip_id": "TP", "route_id": "P1", "shape_id": "SP"},
            ],
            shapes_rows=(
                _shape("SM", METRO_POINTS)
                + _shape("ST", TRAM_POINTS)
                + _shape("SP", TRAIN_POINTS)
            ),
        )

        result = bg.build_geojson(
            zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS, tram_names=SL_TRAM_NAMES
        )

        assert set(result) == {"metro", "tram", "train"}
        for mode, collection in result.items():
            assert collection["type"] == "FeatureCollection"
            assert collection["features"], f"{mode} has no features"
            assert all(f["properties"]["color"] != "#888" for f in collection["features"])

    def test_missing_shapes_file_returns_empty_dict(self):
        """A feed without shapes.txt yields no GeoJSON rather than crashing."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"}],
            shapes_rows=None,
        )

        assert bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS) == {}

    def test_no_rail_routes_returns_empty_dict(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "B1", "route_short_name": "4", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "B1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", METRO_POINTS),
        )

        assert bg.build_geojson(zf) == {}


class TestGeometry:
    """LineString geometry: order, orientation and simplification."""

    def test_coordinates_are_lon_lat_and_sequence_ordered(self):
        """GeoJSON is [lon, lat]; points must follow shape_pt_sequence."""
        rows = _shape("S1", METRO_POINTS)
        rows.reverse()  # deliberately out of sequence order in the file
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"}],
            shapes_rows=rows,
        )

        coords = bg.build_geojson(zf)["metro"]["features"][0]["geometry"]["coordinates"]

        assert coords[0] == [18.0, 59.3]
        assert coords[-1] == [18.15, 59.34]

    def test_geometry_type_is_linestring(self):
        geometry = bg.build_geojson(_metro_zip())["metro"]["features"][0]["geometry"]
        assert geometry["type"] == "LineString"

    def test_opposite_direction_shape_is_deduplicated(self):
        """The same track drawn in reverse must not produce a second feature."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[
                {"trip_id": "T1", "route_id": "M1", "shape_id": "S1"},
                {"trip_id": "T2", "route_id": "M1", "shape_id": "S2"},
            ],
            shapes_rows=_shape("S1", METRO_POINTS) + _shape("S2", list(reversed(METRO_POINTS))),
        )

        result = bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS)

        assert len(result["metro"]["features"]) == 1

    def test_distinct_branches_of_same_line_both_kept(self):
        """Two genuinely different directions for one line give two features."""
        other_branch = [(59.3000, 18.0000), (59.2800, 17.9000), (59.2500, 17.8000)]
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[
                {"trip_id": "T1", "route_id": "M1", "shape_id": "S1"},
                {"trip_id": "T2", "route_id": "M1", "shape_id": "S2"},
            ],
            shapes_rows=_shape("S1", METRO_POINTS) + _shape("S2", other_branch),
        )

        result = bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS)

        features = result["metro"]["features"]
        assert len(features) == 2
        # Both still carry the full property set
        assert all(f["properties"]["color"] == "#007db8" for f in features)

    def test_output_is_json_serializable(self):
        result = bg.build_geojson(
            _metro_zip(), metro_groups=SL_METRO_GROUPS, colors=SL_COLORS
        )
        assert json.loads(json.dumps(result, ensure_ascii=False)) == result


class TestSimplify:
    """Douglas-Peucker helper."""

    def test_short_lines_are_returned_unchanged(self):
        coords = [(18.0, 59.3), (18.1, 59.4)]
        assert bg.simplify(coords) == coords

    def test_collinear_midpoints_are_dropped(self):
        coords = [(18.0, 59.3), (18.1, 59.3), (18.2, 59.3)]
        assert bg.simplify(coords) == [(18.0, 59.3), (18.2, 59.3)]

    def test_significant_bend_is_kept(self):
        coords = [(18.0, 59.3), (18.1, 59.5), (18.2, 59.3)]
        assert len(bg.simplify(coords)) == 3


# ---------------------------------------------------------------------------
# main(): config loading, --gtfs-zip, output files
# ---------------------------------------------------------------------------

def _write_config(path, transport):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"transport": transport}, f, ensure_ascii=False)
    return str(path)


def _full_rail_zip(path):
    return _gtfs_zip_file(
        path,
        routes_rows=[
            {"route_id": "M1", "route_short_name": "10", "route_type": "401"},
            {"route_id": "TR1", "route_short_name": "30", "route_type": "900"},
            {"route_id": "P1", "route_short_name": "41", "route_type": "100"},
        ],
        trips_rows=[
            {"trip_id": "TM", "route_id": "M1", "shape_id": "SM"},
            {"trip_id": "TT", "route_id": "TR1", "shape_id": "ST"},
            {"trip_id": "TP", "route_id": "P1", "shape_id": "SP"},
        ],
        shapes_rows=(
            _shape("SM", METRO_POINTS)
            + _shape("ST", TRAM_POINTS)
            + _shape("SP", TRAIN_POINTS)
        ),
    )


class TestMainConfigLoading:
    """main() must thread metro_groups/colors/tram_names from config.json."""

    def test_config_values_reach_the_written_files(self, tmp_path):
        """End-to-end: config.json colours show up in the GeoJSON on disk."""
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")
        config_path = _write_config(tmp_path / "config.json", {
            "metro_groups": SL_METRO_GROUPS,
            "colors": SL_COLORS,
            "tram_names": SL_TRAM_NAMES,
        })
        out_dir = tmp_path / "out"

        with patch("sys.argv", [
            "build_geojson.py",
            "--gtfs-zip", zip_path,
            "--config", config_path,
            "--output-dir", str(out_dir),
        ]):
            bg.main()

        metro = json.loads((out_dir / "metro_lines.geojson").read_text(encoding="utf-8"))
        tram = json.loads((out_dir / "tram_lines.geojson").read_text(encoding="utf-8"))
        train = json.loads((out_dir / "train_lines.geojson").read_text(encoding="utf-8"))

        assert metro["features"][0]["properties"] == {
            "name": "Linje 10", "ref": "10", "group": "blue", "color": "#007db8",
        }
        assert tram["features"][0]["properties"] == {
            "name": "Tvärbanan", "ref": "30", "color": "#1a5276",
        }
        assert train["features"][0]["properties"] == {
            "name": "Pendeltåg 41", "ref": "41", "color": "#8e44ad",
        }

    def test_config_dicts_are_passed_to_build_geojson(self, tmp_path):
        """The three transport keys are forwarded as keyword arguments."""
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")
        config_path = _write_config(tmp_path / "config.json", {
            "metro_groups": SL_METRO_GROUPS,
            "colors": SL_COLORS,
            "tram_names": SL_TRAM_NAMES,
        })

        with patch("build_geojson.build_geojson", return_value={}) as mock_build:
            with patch("sys.argv", [
                "build_geojson.py",
                "--gtfs-zip", zip_path,
                "--config", config_path,
                "--output-dir", str(tmp_path / "out"),
            ]):
                bg.main()

        kwargs = mock_build.call_args.kwargs
        assert kwargs["metro_groups"] == SL_METRO_GROUPS
        assert kwargs["colors"] == SL_COLORS
        assert kwargs["tram_names"] == SL_TRAM_NAMES

    def test_missing_config_file_yields_empty_dicts(self, tmp_path):
        """A nonexistent config path must not crash — everything defaults empty."""
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")

        with patch("build_geojson.build_geojson", return_value={}) as mock_build:
            with patch("sys.argv", [
                "build_geojson.py",
                "--gtfs-zip", zip_path,
                "--config", str(tmp_path / "nope.json"),
                "--output-dir", str(tmp_path / "out"),
            ]):
                bg.main()

        kwargs = mock_build.call_args.kwargs
        assert kwargs == {"metro_groups": {}, "colors": {}, "tram_names": {}}

    def test_config_without_transport_section_yields_empty_dicts(self, tmp_path):
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w") as f:
            json.dump({"name": "Testregion"}, f)

        with patch("build_geojson.build_geojson", return_value={}) as mock_build:
            with patch("sys.argv", [
                "build_geojson.py",
                "--gtfs-zip", zip_path,
                "--config", config_path,
                "--output-dir", str(tmp_path / "out"),
            ]):
                bg.main()

        assert mock_build.call_args.kwargs == {"metro_groups": {}, "colors": {}, "tram_names": {}}


class TestMainGtfsZipFlag:
    """--gtfs-zip reuses an already-downloaded snapshot (Trafiklab 429 fix)."""

    def test_gtfs_zip_skips_download(self, tmp_path):
        """No API call may happen when a local zip is supplied."""
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")

        with patch("build_geojson.download_gtfs") as mock_download:
            with patch("sys.argv", [
                "build_geojson.py",
                "--gtfs-zip", zip_path,
                "--config", str(tmp_path / "missing.json"),
                "--output-dir", str(tmp_path / "out"),
            ]):
                bg.main()

        mock_download.assert_not_called()

    def test_gtfs_zip_works_without_any_api_key(self, tmp_path):
        """--gtfs-zip must not hit the 'no API key' exit path."""
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")
        out_dir = tmp_path / "out"

        with patch("build_geojson._read_key", return_value="") as mock_key:
            with patch("sys.argv", [
                "build_geojson.py",
                "--gtfs-zip", zip_path,
                "--config", str(tmp_path / "missing.json"),
                "--output-dir", str(out_dir),
            ]):
                bg.main()  # must not raise SystemExit

        mock_key.assert_not_called()
        assert (out_dir / "metro_lines.geojson").exists()

    def test_download_used_when_no_local_zip(self, tmp_path):
        """Without --gtfs-zip the script still downloads with the given key."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", METRO_POINTS),
        )

        with patch("build_geojson.download_gtfs", return_value=zf) as mock_download:
            with patch("sys.argv", [
                "build_geojson.py",
                "--key", "test-key",
                "--config", str(tmp_path / "missing.json"),
                "--output-dir", str(tmp_path / "out"),
            ]):
                bg.main()

        mock_download.assert_called_once_with("test-key", operator="sl")

    def test_exits_without_key_and_without_local_zip(self, tmp_path):
        """No key and no local zip is a hard error, not a silent empty build."""
        with patch("build_geojson._read_key", return_value=""):
            with patch("sys.argv", [
                "build_geojson.py",
                "--config", str(tmp_path / "missing.json"),
                "--output-dir", str(tmp_path / "out"),
            ]):
                with pytest.raises(SystemExit) as exc_info:
                    bg.main()

        assert exc_info.value.code == 1


class TestMainOutput:
    """File naming and behaviour when there is nothing to write."""

    def test_uses_expected_filenames(self, tmp_path):
        zip_path = _full_rail_zip(tmp_path / "gtfs.zip")
        out_dir = tmp_path / "out"

        with patch("sys.argv", [
            "build_geojson.py",
            "--gtfs-zip", zip_path,
            "--config", str(tmp_path / "missing.json"),
            "--output-dir", str(out_dir),
        ]):
            bg.main()

        assert sorted(os.listdir(out_dir)) == [
            "metro_lines.geojson", "train_lines.geojson", "tram_lines.geojson",
        ]

    def test_no_rail_data_writes_no_files(self, tmp_path):
        zip_path = _gtfs_zip_file(
            tmp_path / "gtfs.zip",
            routes_rows=[{"route_id": "B1", "route_short_name": "4", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "B1", "shape_id": "S1"}],
            shapes_rows=_shape("S1", METRO_POINTS),
        )
        out_dir = tmp_path / "out"

        with patch("sys.argv", [
            "build_geojson.py",
            "--gtfs-zip", zip_path,
            "--config", str(tmp_path / "missing.json"),
            "--output-dir", str(out_dir),
        ]):
            bg.main()

        assert os.listdir(out_dir) == []


# ---------------------------------------------------------------------------
# Shape deduplication (review 2026-09-21, finding 9)
# ---------------------------------------------------------------------------

class TestShapeDeduplication:
    """Two alignments that share terminals are not the same track.

    The key was the first and last point rounded to two decimals — over a
    kilometre at this latitude — so a branch or a diverted stretch with the
    same end stations was discarded as a duplicate. The map then lacked that
    alignment, and snap-to-track pulled its vehicles onto the wrong rails.
    """

    # Same start and end, clearly different middles.
    _WEST = [(59.3000, 18.0000), (59.3200, 17.9000), (59.3400, 18.1000)]
    _EAST = [(59.3000, 18.0000), (59.3200, 18.2000), (59.3400, 18.1000)]

    def _build(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "13",
                          "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"},
                        {"trip_id": "T2", "route_id": "M1", "shape_id": "S2"}],
            shapes_rows=_shape("S1", self._WEST) + _shape("S2", self._EAST),
        )
        return bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS,
                                tram_names=SL_TRAM_NAMES)

    def test_both_alignments_survive(self):
        """Rounded endpoints made these identical; the geometry does not."""
        out = self._build()
        feats = out["metro"]["features"]
        assert len(feats) == 2, (
            f"expected both alignments, kept {len(feats)} — the dedup key is "
            "still comparing endpoints rather than geometry"
        )

    def test_the_two_kept_shapes_are_actually_different(self):
        out = self._build()
        geoms = [tuple(map(tuple, f["geometry"]["coordinates"]))
                 for f in out["metro"]["features"]]
        assert geoms[0] != geoms[1]

    def test_an_exact_duplicate_is_still_dropped(self):
        """The dedup must still do its job for genuinely identical shapes."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "13",
                          "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"},
                        {"trip_id": "T2", "route_id": "M1", "shape_id": "S2"}],
            shapes_rows=_shape("S1", self._WEST) + _shape("S2", self._WEST),
        )
        out = bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS,
                               tram_names=SL_TRAM_NAMES)
        assert len(out["metro"]["features"]) == 1

    def test_the_same_shape_reversed_is_still_dropped(self):
        """One track travelled both ways is one track."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "13",
                          "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1", "shape_id": "S1"},
                        {"trip_id": "T2", "route_id": "M1", "shape_id": "S2"}],
            shapes_rows=_shape("S1", self._WEST)
            + _shape("S2", list(reversed(self._WEST))),
        )
        out = bg.build_geojson(zf, metro_groups=SL_METRO_GROUPS, colors=SL_COLORS,
                               tram_names=SL_TRAM_NAMES)
        assert len(out["metro"]["features"]) == 1
