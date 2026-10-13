"""Tests for build_stations.py — GTFS Static → stations.json.

P1 — stations.json drives the metro/tram/train station dots and labels on the
map. Wrong or missing `group` values mean stations cannot be filtered per
metro branch; missing coordinates mean stations vanish.

This file was previously untested; it shares the CLI shape (--config,
--gtfs-zip) with build_geojson.py, so the same config-threading and
"do not download twice" guarantees are covered here.
"""

import csv
import io
import json
import zipfile
from unittest.mock import patch

import pytest

import build_stations as bs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROUTE_FIELDS = ["route_id", "route_short_name", "route_type"]
TRIP_FIELDS = ["trip_id", "route_id"]
STOP_TIME_FIELDS = ["trip_id", "stop_id", "stop_sequence"]
STOP_FIELDS = ["stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"]


def _write_csv(zf, name, fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    zf.writestr(name, buf.getvalue())


def _gtfs_bytes(routes_rows, trips_rows, stop_times_rows, stops_rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        _write_csv(zf, "routes.txt", ROUTE_FIELDS, routes_rows)
        _write_csv(zf, "trips.txt", TRIP_FIELDS, trips_rows)
        _write_csv(zf, "stop_times.txt", STOP_TIME_FIELDS, stop_times_rows)
        _write_csv(zf, "stops.txt", STOP_FIELDS, stops_rows)
    return buf.getvalue()


def _gtfs_zip(routes_rows, trips_rows, stop_times_rows, stops_rows):
    return zipfile.ZipFile(
        io.BytesIO(_gtfs_bytes(routes_rows, trips_rows, stop_times_rows, stops_rows)), "r"
    )


def _gtfs_zip_file(path, **kwargs):
    with open(path, "wb") as f:
        f.write(_gtfs_bytes(**kwargs))
    return str(path)


def _stop(stop_id, name, lat, lon, parent=""):
    return {
        "stop_id": stop_id,
        "stop_name": name,
        "stop_lat": str(lat),
        "stop_lon": str(lon),
        "parent_station": parent,
    }


SL_METRO_GROUPS = {"10": "blue", "13": "red", "17": "green"}


def _simple_metro_kwargs():
    """One metro line 10 serving one stop."""
    return dict(
        routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
        trips_rows=[{"trip_id": "T1", "route_id": "M1"}],
        stop_times_rows=[{"trip_id": "T1", "stop_id": "S1", "stop_sequence": "1"}],
        stops_rows=[_stop("S1", "Kungsträdgården", 59.3306, 18.0722)],
    )


# ---------------------------------------------------------------------------
# build_stations
# ---------------------------------------------------------------------------

class TestBuildStations:
    """Core extraction of rail stations from a GTFS feed."""

    def test_metro_station_has_name_and_coordinates(self):
        result = bs.build_stations(_gtfs_zip(**_simple_metro_kwargs()))

        assert list(result) == ["metro"]
        station = result["metro"][0]
        assert station["name"] == "Kungsträdgården"
        assert station["lat"] == pytest.approx(59.3306)
        assert station["lon"] == pytest.approx(18.0722)

    def test_metro_group_comes_from_config(self):
        """Line 10 is blue, so its stations are tagged with the blue group."""
        result = bs.build_stations(
            _gtfs_zip(**_simple_metro_kwargs()), metro_groups=SL_METRO_GROUPS
        )

        assert result["metro"][0]["group"] == "blue"

    def test_unknown_metro_line_gets_no_group(self):
        kwargs = _simple_metro_kwargs()
        kwargs["routes_rows"] = [
            {"route_id": "M1", "route_short_name": "99", "route_type": "401"}
        ]
        result = bs.build_stations(_gtfs_zip(**kwargs), metro_groups=SL_METRO_GROUPS)

        assert "group" not in result["metro"][0]

    def test_bus_only_stops_are_excluded(self):
        """Bus stops must not end up in stations.json."""
        zf = _gtfs_zip(
            routes_rows=[
                {"route_id": "B1", "route_short_name": "4", "route_type": "700"},
                {"route_id": "M1", "route_short_name": "10", "route_type": "401"},
            ],
            trips_rows=[
                {"trip_id": "TB", "route_id": "B1"},
                {"trip_id": "TM", "route_id": "M1"},
            ],
            stop_times_rows=[
                {"trip_id": "TB", "stop_id": "SB", "stop_sequence": "1"},
                {"trip_id": "TM", "stop_id": "SM", "stop_sequence": "1"},
            ],
            stops_rows=[
                _stop("SB", "Busshållplats", 59.30, 18.00),
                _stop("SM", "T-Centralen", 59.3311, 18.0594),
            ],
        )

        result = bs.build_stations(zf)

        assert [s["name"] for s in result["metro"]] == ["T-Centralen"]

    def test_platforms_aggregate_to_parent_station(self):
        """Two platforms with the same parent produce one station."""
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1"}],
            stop_times_rows=[
                {"trip_id": "T1", "stop_id": "P1", "stop_sequence": "1"},
                {"trip_id": "T1", "stop_id": "P2", "stop_sequence": "2"},
            ],
            stops_rows=[
                _stop("PARENT", "Slussen", 59.3196, 18.0719),
                _stop("P1", "Slussen läge 1", 59.3196, 18.0719, parent="PARENT"),
                _stop("P2", "Slussen läge 2", 59.3197, 18.0720, parent="PARENT"),
            ],
        )

        result = bs.build_stations(zf)

        assert len(result["metro"]) == 1
        assert result["metro"][0]["name"] == "Slussen"

    def test_duplicate_names_within_a_mode_are_dropped(self):
        zf = _gtfs_zip(
            routes_rows=[{"route_id": "M1", "route_short_name": "10", "route_type": "401"}],
            trips_rows=[{"trip_id": "T1", "route_id": "M1"}],
            stop_times_rows=[
                {"trip_id": "T1", "stop_id": "A", "stop_sequence": "1"},
                {"trip_id": "T1", "stop_id": "B", "stop_sequence": "2"},
            ],
            stops_rows=[
                _stop("A", "Gullmarsplan", 59.2986, 18.0806),
                _stop("B", "Gullmarsplan", 59.2987, 18.0807),
            ],
        )

        result = bs.build_stations(zf)

        assert len(result["metro"]) == 1

    def test_stops_without_coordinates_are_skipped(self):
        kwargs = _simple_metro_kwargs()
        kwargs["stops_rows"] = [_stop("S1", "Utan koordinater", 0, 0)]

        assert bs.build_stations(_gtfs_zip(**kwargs)) == {}

    def test_all_rail_modes_are_grouped_separately(self):
        zf = _gtfs_zip(
            routes_rows=[
                {"route_id": "M1", "route_short_name": "10", "route_type": "401"},
                {"route_id": "TR1", "route_short_name": "30", "route_type": "900"},
                {"route_id": "P1", "route_short_name": "41", "route_type": "100"},
            ],
            trips_rows=[
                {"trip_id": "TM", "route_id": "M1"},
                {"trip_id": "TT", "route_id": "TR1"},
                {"trip_id": "TP", "route_id": "P1"},
            ],
            stop_times_rows=[
                {"trip_id": "TM", "stop_id": "SM", "stop_sequence": "1"},
                {"trip_id": "TT", "stop_id": "ST", "stop_sequence": "1"},
                {"trip_id": "TP", "stop_id": "SP", "stop_sequence": "1"},
            ],
            stops_rows=[
                _stop("SM", "Rådhuset", 59.3306, 18.0364),
                _stop("ST", "Alvik", 59.3336, 17.9789),
                _stop("SP", "Södertälje hamn", 59.1966, 17.6295),
            ],
        )

        result = bs.build_stations(zf, metro_groups=SL_METRO_GROUPS)

        assert set(result) == {"metro", "tram", "train"}
        assert result["metro"][0]["group"] == "blue"
        # Non-metro modes never get a group key
        assert "group" not in result["tram"][0]
        assert "group" not in result["train"][0]


# ---------------------------------------------------------------------------
# main(): config, --gtfs-zip, output
# ---------------------------------------------------------------------------

class TestMain:
    """CLI behaviour: config loading, local zip reuse, written output."""

    def test_writes_stations_json_with_groups_from_config(self, tmp_path):
        zip_path = _gtfs_zip_file(tmp_path / "gtfs.zip", **_simple_metro_kwargs())
        config_path = str(tmp_path / "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"transport": {"metro_groups": SL_METRO_GROUPS}}, f, ensure_ascii=False)
        output = str(tmp_path / "stations.json")

        with patch("sys.argv", [
            "build_stations.py",
            "--gtfs-zip", zip_path,
            "--config", config_path,
            "--output", output,
        ]):
            bs.main()

        data = json.loads(open(output, encoding="utf-8").read())
        assert data["metro"][0]["name"] == "Kungsträdgården"
        assert data["metro"][0]["group"] == "blue"

    def test_gtfs_zip_skips_download(self, tmp_path):
        """The shared-snapshot flag must not trigger a second Trafiklab call."""
        zip_path = _gtfs_zip_file(tmp_path / "gtfs.zip", **_simple_metro_kwargs())

        with patch("build_stations.download_gtfs") as mock_download:
            with patch("sys.argv", [
                "build_stations.py",
                "--gtfs-zip", zip_path,
                "--config", str(tmp_path / "missing.json"),
                "--output", str(tmp_path / "stations.json"),
            ]):
                bs.main()

        mock_download.assert_not_called()

    def test_gtfs_zip_works_without_api_key(self, tmp_path):
        zip_path = _gtfs_zip_file(tmp_path / "gtfs.zip", **_simple_metro_kwargs())
        output = tmp_path / "stations.json"

        with patch("build_stations._read_key", return_value="") as mock_key:
            with patch("sys.argv", [
                "build_stations.py",
                "--gtfs-zip", zip_path,
                "--config", str(tmp_path / "missing.json"),
                "--output", str(output),
            ]):
                bs.main()  # must not raise SystemExit

        mock_key.assert_not_called()
        assert output.exists()

    def test_download_used_when_no_local_zip(self, tmp_path):
        with patch("build_stations.download_gtfs", return_value=_gtfs_zip(**_simple_metro_kwargs())) as mock_download:
            with patch("sys.argv", [
                "build_stations.py",
                "--key", "test-key",
                "--config", str(tmp_path / "missing.json"),
                "--output", str(tmp_path / "stations.json"),
            ]):
                bs.main()

        mock_download.assert_called_once_with("test-key", operator="sl")

    def test_exits_without_key_and_without_local_zip(self, tmp_path):
        with patch("build_stations._read_key", return_value=""):
            with patch("sys.argv", [
                "build_stations.py",
                "--config", str(tmp_path / "missing.json"),
                "--output", str(tmp_path / "stations.json"),
            ]):
                with pytest.raises(SystemExit) as exc_info:
                    bs.main()

        assert exc_info.value.code == 1

    def test_missing_config_still_writes_file_without_groups(self, tmp_path):
        zip_path = _gtfs_zip_file(tmp_path / "gtfs.zip", **_simple_metro_kwargs())
        output = str(tmp_path / "stations.json")

        with patch("sys.argv", [
            "build_stations.py",
            "--gtfs-zip", zip_path,
            "--config", str(tmp_path / "missing.json"),
            "--output", output,
        ]):
            bs.main()

        data = json.loads(open(output, encoding="utf-8").read())
        assert "group" not in data["metro"][0]
