"""Tests for build_trip_lookup.py — GTFS Static data parser.

P1 — The trip_lookup.json file is critical for mapping vehicle trip_ids to
human-readable line names, route types, and headsigns. Without correct data,
the map shows vehicles without labels. This script rebuilds the file from
GTFS Static data, so we test the core logic: CSV parsing, deduplication, and
output format — without calling external APIs.
"""

import csv
import io
import json
import os
import zipfile
from unittest.mock import patch, MagicMock

import pytest

import build_trip_lookup as btl


# ---------------------------------------------------------------------------
# Helpers: build in-memory GTFS zip files
# ---------------------------------------------------------------------------

def _make_gtfs_zip(routes_rows, trips_rows, stop_times_rows=None):
    """Create an in-memory ZipFile with GTFS CSV files.

    Args:
        routes_rows: list of dicts with route_id, route_short_name, route_type
        trips_rows: list of dicts with trip_id, route_id, trip_headsign
        stop_times_rows: optional list of dicts with trip_id, stop_headsign, etc.

    Returns:
        zipfile.ZipFile opened in read mode
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # routes.txt
        routes_csv = io.StringIO()
        writer = csv.DictWriter(routes_csv, fieldnames=["route_id", "route_short_name", "route_type"])
        writer.writeheader()
        for r in routes_rows:
            writer.writerow(r)
        zf.writestr("routes.txt", routes_csv.getvalue())

        # trips.txt
        trips_csv = io.StringIO()
        writer = csv.DictWriter(trips_csv, fieldnames=["trip_id", "route_id", "trip_headsign"])
        writer.writeheader()
        for t in trips_rows:
            writer.writerow(t)
        zf.writestr("trips.txt", trips_csv.getvalue())

        # stop_times.txt (optional)
        if stop_times_rows is not None:
            st_csv = io.StringIO()
            writer = csv.DictWriter(st_csv, fieldnames=["trip_id", "stop_headsign", "stop_sequence"])
            writer.writeheader()
            for st in stop_times_rows:
                writer.writerow(st)
            zf.writestr("stop_times.txt", st_csv.getvalue())

    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


# ---------------------------------------------------------------------------
# read_csv
# ---------------------------------------------------------------------------

class TestReadCsv:
    """Tests for read_csv: reading CSV files from zip archives."""

    def test_reads_csv_from_zip(self):
        """read_csv should return a list of dicts from a CSV file in a zip."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[],
        )
        rows = btl.read_csv(zf, "routes.txt")
        assert len(rows) == 1
        assert rows[0]["route_id"] == "R1"
        assert rows[0]["route_short_name"] == "1"
        assert rows[0]["route_type"] == "700"

    def test_handles_bom_encoding(self):
        """CSV files with UTF-8 BOM should be parsed correctly."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # Write CSV with BOM prefix (encode as utf-8-sig adds the BOM bytes)
            content = "route_id,route_short_name,route_type\nR1,BOM-test,700\n"
            zf.writestr("routes.txt", content.encode("utf-8-sig"))
        buf.seek(0)
        zf = zipfile.ZipFile(buf, "r")

        rows = btl.read_csv(zf, "routes.txt")
        assert len(rows) == 1
        assert rows[0]["route_id"] == "R1"

    def test_empty_csv_returns_empty_list(self):
        """A CSV file with only headers should return an empty list."""
        zf = _make_gtfs_zip(routes_rows=[], trips_rows=[])
        rows = btl.read_csv(zf, "routes.txt")
        assert rows == []

    def test_multiple_rows(self):
        """Multiple rows are all returned."""
        routes = [
            {"route_id": f"R{i}", "route_short_name": str(i), "route_type": "700"}
            for i in range(5)
        ]
        zf = _make_gtfs_zip(routes_rows=routes, trips_rows=[])
        rows = btl.read_csv(zf, "routes.txt")
        assert len(rows) == 5


# ---------------------------------------------------------------------------
# build_lookup: basic output format
# ---------------------------------------------------------------------------

class TestBuildLookupFormat:
    """Tests for the output structure of build_lookup."""

    def test_output_has_required_keys(self):
        """Output must have routes, lines, headsigns, and trips keys."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Centralen"}],
        )
        result = btl.build_lookup(zf)

        assert "routes" in result
        assert "lines" in result
        assert "headsigns" in result
        assert "trips" in result

    def test_output_types(self):
        """routes, lines, headsigns should be lists; trips should be a dict."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Centralen"}],
        )
        result = btl.build_lookup(zf)

        assert isinstance(result["routes"], list)
        assert isinstance(result["lines"], list)
        assert isinstance(result["headsigns"], list)
        assert isinstance(result["trips"], dict)

    def test_trip_entry_format(self):
        """Each trip should be [routeIdx, lineIdx, routeType, headsignIdx]."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "3", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Södersjukhuset"}],
        )
        result = btl.build_lookup(zf)

        assert "T1" in result["trips"]
        entry = result["trips"]["T1"]
        assert len(entry) == 4
        route_idx, line_idx, route_type, headsign_idx = entry
        assert isinstance(route_idx, int)
        assert isinstance(line_idx, int)
        assert route_type == 700
        assert isinstance(headsign_idx, int)

    def test_trip_entry_indexes_resolve_correctly(self):
        """Indexes in trip entries should point to correct values in arrays."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "3", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Södersjukhuset"}],
        )
        result = btl.build_lookup(zf)

        entry = result["trips"]["T1"]
        route_idx, line_idx, route_type, headsign_idx = entry

        assert result["routes"][route_idx] == "R1"
        assert result["lines"][line_idx] == "3"
        assert result["headsigns"][headsign_idx] == "Södersjukhuset"


# ---------------------------------------------------------------------------
# build_lookup: deduplication
# ---------------------------------------------------------------------------

class TestBuildLookupDeduplication:
    """Tests for deduplication of routes, lines, and headsigns."""

    def test_same_route_deduplicated(self):
        """Multiple trips on the same route should share one route index."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "3", "route_type": "700"}],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "A"},
                {"trip_id": "T2", "route_id": "R1", "trip_headsign": "B"},
            ],
        )
        result = btl.build_lookup(zf)

        assert len(result["routes"]) == 1
        assert result["trips"]["T1"][0] == result["trips"]["T2"][0]

    def test_same_line_name_deduplicated(self):
        """Different routes with the same line name share one line index."""
        zf = _make_gtfs_zip(
            routes_rows=[
                {"route_id": "R1", "route_short_name": "3", "route_type": "700"},
                {"route_id": "R2", "route_short_name": "3", "route_type": "700"},
            ],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "A"},
                {"trip_id": "T2", "route_id": "R2", "trip_headsign": "B"},
            ],
        )
        result = btl.build_lookup(zf)

        assert len(result["lines"]) == 1
        assert result["lines"][0] == "3"
        assert result["trips"]["T1"][1] == result["trips"]["T2"][1]

    def test_same_headsign_deduplicated(self):
        """Trips with the same headsign share one headsign index."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "Centralen"},
                {"trip_id": "T2", "route_id": "R1", "trip_headsign": "Centralen"},
            ],
        )
        result = btl.build_lookup(zf)

        assert len(result["headsigns"]) == 1
        assert result["headsigns"][0] == "Centralen"
        assert result["trips"]["T1"][3] == result["trips"]["T2"][3]

    def test_different_values_not_deduplicated(self):
        """Distinct routes, lines, and headsigns should each get their own index."""
        zf = _make_gtfs_zip(
            routes_rows=[
                {"route_id": "R1", "route_short_name": "1", "route_type": "700"},
                {"route_id": "R2", "route_short_name": "2", "route_type": "401"},
            ],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "Norsborg"},
                {"trip_id": "T2", "route_id": "R2", "trip_headsign": "Fruängen"},
            ],
        )
        result = btl.build_lookup(zf)

        assert len(result["routes"]) == 2
        assert len(result["lines"]) == 2
        assert len(result["headsigns"]) == 2

    def test_large_dataset_deduplication(self):
        """Deduplication should work correctly with many trips on few routes."""
        routes = [{"route_id": "R1", "route_short_name": "42", "route_type": "700"}]
        trips = [
            {"trip_id": f"T{i}", "route_id": "R1", "trip_headsign": "Destination"}
            for i in range(100)
        ]
        zf = _make_gtfs_zip(routes_rows=routes, trips_rows=trips)
        result = btl.build_lookup(zf)

        assert len(result["routes"]) == 1
        assert len(result["lines"]) == 1
        assert len(result["headsigns"]) == 1
        assert len(result["trips"]) == 100


# ---------------------------------------------------------------------------
# build_lookup: skipping trips without line name
# ---------------------------------------------------------------------------

class TestBuildLookupSkipNoLine:
    """Trips linked to routes without route_short_name should be skipped."""

    def test_trips_without_line_name_are_skipped(self):
        """A route with empty route_short_name means the trip has no visible line."""
        zf = _make_gtfs_zip(
            routes_rows=[
                {"route_id": "R1", "route_short_name": "", "route_type": "700"},
                {"route_id": "R2", "route_short_name": "3", "route_type": "700"},
            ],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "Unknown"},
                {"trip_id": "T2", "route_id": "R2", "trip_headsign": "Known"},
            ],
        )
        result = btl.build_lookup(zf)

        assert "T1" not in result["trips"]
        assert "T2" in result["trips"]
        assert len(result["trips"]) == 1

    def test_trips_with_unknown_route_are_skipped(self):
        """If a trip references a route_id not in routes.txt, it is skipped."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R_MISSING", "trip_headsign": "Nowhere"},
                {"trip_id": "T2", "route_id": "R1", "trip_headsign": "Somewhere"},
            ],
        )
        result = btl.build_lookup(zf)

        assert "T1" not in result["trips"]
        assert "T2" in result["trips"]

    def test_all_trips_skipped_produces_empty_result(self):
        """If all trips lack line names, the result should have empty arrays."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "X"}],
        )
        result = btl.build_lookup(zf)

        assert result["routes"] == []
        assert result["lines"] == []
        assert result["headsigns"] == []
        assert result["trips"] == {}


# ---------------------------------------------------------------------------
# build_lookup: headsign fallback from stop_times.txt
# ---------------------------------------------------------------------------

class TestBuildLookupHeadsignFallback:
    """When trip_headsign is empty, build_lookup should try stop_headsign."""

    def test_stop_headsign_used_when_trip_headsign_empty(self):
        """Trips without trip_headsign get headsign from stop_times.txt."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": ""}],
            stop_times_rows=[
                {"trip_id": "T1", "stop_headsign": "Fallback Dest", "stop_sequence": "1"},
            ],
        )
        result = btl.build_lookup(zf)

        assert "T1" in result["trips"]
        headsign_idx = result["trips"]["T1"][3]
        assert result["headsigns"][headsign_idx] == "Fallback Dest"

    def test_trip_headsign_preferred_over_stop_headsign(self):
        """When trip_headsign exists, stop_headsign should not override it."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Original"}],
            stop_times_rows=[
                {"trip_id": "T1", "stop_headsign": "Should Not Override", "stop_sequence": "1"},
            ],
        )
        result = btl.build_lookup(zf)

        headsign_idx = result["trips"]["T1"][3]
        assert result["headsigns"][headsign_idx] == "Original"

    def test_still_works_without_stop_times_file(self):
        """If stop_times.txt is not in the zip, headsign stays empty."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": ""}],
            stop_times_rows=None,  # No stop_times.txt
        )
        result = btl.build_lookup(zf)

        # Trip should still be included (empty headsign is ok, line name is present)
        assert "T1" in result["trips"]
        headsign_idx = result["trips"]["T1"][3]
        # -1 means no headsign
        assert headsign_idx == -1

    def test_multiple_stop_headsigns_first_wins(self):
        """When stop_times has multiple entries for a trip, the first non-empty wins."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": ""}],
            stop_times_rows=[
                {"trip_id": "T1", "stop_headsign": "First Stop", "stop_sequence": "1"},
                {"trip_id": "T1", "stop_headsign": "Second Stop", "stop_sequence": "2"},
            ],
        )
        result = btl.build_lookup(zf)

        headsign_idx = result["trips"]["T1"][3]
        assert result["headsigns"][headsign_idx] == "First Stop"

    def test_empty_stop_headsign_is_ignored(self):
        """stop_headsign entries that are empty strings should not fill in the gap."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": ""}],
            stop_times_rows=[
                {"trip_id": "T1", "stop_headsign": "", "stop_sequence": "1"},
            ],
        )
        result = btl.build_lookup(zf)

        # Headsign should still be empty (index -1)
        headsign_idx = result["trips"]["T1"][3]
        assert headsign_idx == -1


# ---------------------------------------------------------------------------
# build_lookup: route_type handling
# ---------------------------------------------------------------------------

class TestBuildLookupRouteTypes:
    """Route types should be passed through as integers."""

    @pytest.mark.parametrize("route_type,expected", [
        ("100", 100),    # TRAIN
        ("401", 401),    # METRO
        ("700", 700),    # BUS
        ("900", 900),    # TRAM
        ("1000", 1000),  # SHIP
    ])
    def test_route_type_passed_as_integer(self, route_type, expected):
        """Each GTFS route_type should become the correct integer."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": route_type}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Dest"}],
        )
        result = btl.build_lookup(zf)
        assert result["trips"]["T1"][2] == expected

    def test_default_route_type_when_missing(self):
        """When route_type is missing from routes.txt, it defaults to 700 (BUS)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # routes.txt without route_type column
            zf.writestr("routes.txt", "route_id,route_short_name\nR1,99\n")
            zf.writestr("trips.txt", "trip_id,route_id,trip_headsign\nT1,R1,Dest\n")
        buf.seek(0)
        zf = zipfile.ZipFile(buf, "r")

        result = btl.build_lookup(zf)
        assert result["trips"]["T1"][2] == 700


# ---------------------------------------------------------------------------
# build_lookup: headsign index -1 for empty headsign
# ---------------------------------------------------------------------------

class TestBuildLookupEmptyHeadsign:
    """Trips with no headsign (empty string) should get headsign index -1."""

    def test_empty_headsign_gets_minus_one(self):
        """An empty headsign should produce index -1, not index into array."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": ""}],
        )
        result = btl.build_lookup(zf)

        assert result["trips"]["T1"][3] == -1

    def test_mixed_empty_and_nonempty_headsigns(self):
        """A trip with headsign and one without should get different indexes."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "Dest"},
                {"trip_id": "T2", "route_id": "R1", "trip_headsign": ""},
            ],
        )
        result = btl.build_lookup(zf)

        assert result["trips"]["T1"][3] >= 0
        assert result["trips"]["T2"][3] == -1


# ---------------------------------------------------------------------------
# build_lookup: realistic multi-route scenario
# ---------------------------------------------------------------------------

class TestBuildLookupRealistic:
    """Integration-style test with a realistic mix of transport types."""

    def test_multi_type_scenario(self):
        """A realistic mix of bus, metro, and tram routes."""
        routes = [
            {"route_id": "BUS3", "route_short_name": "3", "route_type": "700"},
            {"route_id": "BUS42", "route_short_name": "42", "route_type": "700"},
            {"route_id": "METRO14", "route_short_name": "14", "route_type": "401"},
            {"route_id": "TRAM7", "route_short_name": "7", "route_type": "900"},
            {"route_id": "TRAIN36", "route_short_name": "36", "route_type": "100"},
            {"route_id": "SHIP80", "route_short_name": "80", "route_type": "1000"},
        ]
        trips = [
            {"trip_id": "TB3_1", "route_id": "BUS3", "trip_headsign": "Södersjukhuset"},
            {"trip_id": "TB3_2", "route_id": "BUS3", "trip_headsign": "Karolinska"},
            {"trip_id": "TB42_1", "route_id": "BUS42", "trip_headsign": "Centralen"},
            {"trip_id": "TM14_1", "route_id": "METRO14", "trip_headsign": "Fruängen"},
            {"trip_id": "TM14_2", "route_id": "METRO14", "trip_headsign": "Mörby centrum"},
            {"trip_id": "TT7_1", "route_id": "TRAM7", "trip_headsign": "Djurgården"},
            {"trip_id": "TTR36_1", "route_id": "TRAIN36", "trip_headsign": "Nynäshamn"},
            {"trip_id": "TS80_1", "route_id": "SHIP80", "trip_headsign": "Vaxholm"},
        ]
        zf = _make_gtfs_zip(routes_rows=routes, trips_rows=trips)
        result = btl.build_lookup(zf)

        # 6 distinct routes
        assert len(result["routes"]) == 6
        # 6 distinct line names
        assert len(result["lines"]) == 6
        assert set(result["lines"]) == {"3", "42", "14", "7", "36", "80"}
        # 8 distinct headsigns (all trips have unique destinations)
        assert len(result["headsigns"]) == 8
        # All 8 trips present
        assert len(result["trips"]) == 8

        # Verify a specific trip resolves correctly
        entry = result["trips"]["TM14_1"]
        route_idx, line_idx, route_type, headsign_idx = entry
        assert result["routes"][route_idx] == "METRO14"
        assert result["lines"][line_idx] == "14"
        assert route_type == 401
        assert result["headsigns"][headsign_idx] == "Fruängen"

    def test_output_is_json_serializable(self):
        """The output should be serializable to compact JSON."""
        routes = [{"route_id": "R1", "route_short_name": "1", "route_type": "700"}]
        trips = [{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Dest"}]
        zf = _make_gtfs_zip(routes_rows=routes, trips_rows=trips)
        result = btl.build_lookup(zf)

        # Should not raise
        json_str = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        # Should be valid JSON that roundtrips
        parsed = json.loads(json_str)
        assert parsed == result


# ---------------------------------------------------------------------------
# download_gtfs (mocked)
# ---------------------------------------------------------------------------

class TestDownloadGtfs:
    """Tests for the download_gtfs function with mocked HTTP."""

    @patch("build_trip_lookup.requests.get")
    def test_calls_correct_url_with_key(self, mock_get):
        """download_gtfs should call the Trafiklab URL with the API key."""
        # Create a minimal valid zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("routes.txt", "route_id\n")
        mock_response = MagicMock()
        mock_response.content = buf.getvalue()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = btl.download_gtfs("test-key-123")

        mock_get.assert_called_once_with(
            btl.GTFS_URL_TEMPLATE.format(op="sl"),
            params={"key": "test-key-123"},
            timeout=120,
            headers={"Accept-Encoding": "gzip"},
        )
        assert isinstance(result, zipfile.ZipFile)

    @patch("build_trip_lookup.requests.get")
    def test_raises_on_http_error(self, mock_get):
        """download_gtfs should propagate HTTP errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("403 Forbidden")
        mock_get.return_value = mock_response

        with pytest.raises(Exception, match="403"):
            btl.download_gtfs("bad-key")


# ---------------------------------------------------------------------------
# download_gtfs_to_file (mocked)
# ---------------------------------------------------------------------------

class TestDownloadGtfsToFile:
    """Saves one GTFS snapshot to disk so several build scripts can share it.

    Downloading the same zip once per script tripped Trafiklab's rate limit
    (HTTP 429), which silently left stations/GeoJSON files unrebuilt.
    """

    def _zip_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("routes.txt", "route_id,route_short_name,route_type\nR1,1,401\n")
        return buf.getvalue()

    @patch("build_trip_lookup.requests.get")
    def test_writes_a_readable_zip_to_path(self, mock_get, tmp_path):
        """The saved file must be a valid zip that read_csv can open."""
        mock_response = MagicMock()
        mock_response.content = self._zip_bytes()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        path = str(tmp_path / "gtfs.zip")
        btl.download_gtfs_to_file("test-key-123", path)

        assert os.path.exists(path)
        rows = btl.read_csv(zipfile.ZipFile(path), "routes.txt")
        assert rows[0]["route_id"] == "R1"

    @patch("build_trip_lookup.requests.get")
    def test_calls_correct_url_with_key(self, mock_get, tmp_path):
        mock_response = MagicMock()
        mock_response.content = self._zip_bytes()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        btl.download_gtfs_to_file("test-key-123", str(tmp_path / "gtfs.zip"))

        mock_get.assert_called_once_with(
            btl.GTFS_URL_TEMPLATE.format(op="sl"),
            params={"key": "test-key-123"},
            timeout=120,
            headers={"Accept-Encoding": "gzip"},
        )

    @patch("build_trip_lookup.requests.get")
    def test_operator_selects_the_right_feed(self, mock_get, tmp_path):
        mock_response = MagicMock()
        mock_response.content = self._zip_bytes()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        btl.download_gtfs_to_file("k", str(tmp_path / "gtfs.zip"), operator="skane")

        assert mock_get.call_args.args[0] == btl.GTFS_URL_TEMPLATE.format(op="skane")

    @patch("build_trip_lookup.requests.get")
    def test_no_file_written_on_http_error(self, mock_get, tmp_path):
        """A failed download must not leave a truncated/empty zip behind."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("429 Too Many Requests")
        mock_get.return_value = mock_response

        path = str(tmp_path / "gtfs.zip")
        with pytest.raises(Exception, match="429"):
            btl.download_gtfs_to_file("k", path)

        assert not os.path.exists(path)


# ---------------------------------------------------------------------------
# main: end-to-end with mocked download
# ---------------------------------------------------------------------------

class TestMain:
    """End-to-end test of the main function with mocked download."""

    @patch("build_trip_lookup.download_gtfs")
    def test_writes_output_file(self, mock_download, tmp_path):
        """main should write a valid trip_lookup.json file."""
        zf = _make_gtfs_zip(
            routes_rows=[
                {"route_id": "R1", "route_short_name": "3", "route_type": "700"},
            ],
            trips_rows=[
                {"trip_id": "T1", "route_id": "R1", "trip_headsign": "Centralen"},
            ],
        )
        mock_download.return_value = zf

        output_path = str(tmp_path / "trip_lookup.json")
        with patch("sys.argv", ["build_trip_lookup.py", "--key", "test-key", "--output", output_path]):
            btl.main()

        assert os.path.exists(output_path)
        with open(output_path) as f:
            data = json.load(f)
        assert "routes" in data
        assert "lines" in data
        assert "headsigns" in data
        assert "trips" in data
        assert "T1" in data["trips"]

    @patch("build_trip_lookup.download_gtfs")
    def test_output_is_compact_json(self, mock_download, tmp_path):
        """Output JSON should be compact (no whitespace/indentation)."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "X"}],
        )
        mock_download.return_value = zf

        output_path = str(tmp_path / "trip_lookup.json")
        with patch("sys.argv", ["build_trip_lookup.py", "--key", "k", "--output", output_path]):
            btl.main()

        with open(output_path) as f:
            content = f.read()
        # Compact JSON has no spaces after separators
        assert ": " not in content
        assert ", " not in content

    @patch("build_trip_lookup.os.path.exists", return_value=False)
    def test_exits_without_key(self, mock_exists):
        """main should exit with code 1 if no API key is provided."""
        with patch("sys.argv", ["build_trip_lookup.py"]):
            with patch.dict(os.environ, {"GTFS_STATIC_KEY": ""}, clear=False):
                with pytest.raises(SystemExit) as exc_info:
                    btl.main()
                assert exc_info.value.code == 1

    @patch("build_trip_lookup.download_gtfs")
    def test_reads_key_from_env(self, mock_download, tmp_path):
        """main should accept GTFS_STATIC_KEY from environment."""
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "1", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "X"}],
        )
        mock_download.return_value = zf

        output_path = str(tmp_path / "trip_lookup.json")
        with patch("sys.argv", ["build_trip_lookup.py", "--output", output_path]):
            with patch.dict(os.environ, {"GTFS_STATIC_KEY": "env-key"}):
                btl.main()

        mock_download.assert_called_once_with("env-key", operator="sl")
        assert os.path.exists(output_path)


class TestZipReuse:
    """--gtfs-zip reuses a local zip; --save-zip keeps the downloaded one.

    Trafiklab's GTFS Static quota is tiny (Bronze: 60 calls per 30 days), so
    one download has to serve build_trip_lookup.py, build_stations.py and
    build_geojson.py.
    """

    def _zip_bytes(self):
        zf = _make_gtfs_zip(
            routes_rows=[{"route_id": "R1", "route_short_name": "3", "route_type": "700"}],
            trips_rows=[{"trip_id": "T1", "route_id": "R1", "trip_headsign": "Centralen"}],
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in zf.namelist():
                out.writestr(name, zf.read(name))
        return buf.getvalue()

    @patch("build_trip_lookup.requests.get")
    def test_gtfs_zip_builds_without_download_or_key(self, mock_get, tmp_path):
        zip_path = tmp_path / "sl.zip"
        zip_path.write_bytes(self._zip_bytes())
        output_path = tmp_path / "trip_lookup.json"
        with patch("sys.argv", ["build_trip_lookup.py", "--gtfs-zip", str(zip_path), "--output", str(output_path)]):
            with patch.dict(os.environ, {"GTFS_STATIC_KEY": ""}):
                with patch("build_trip_lookup._read_key", return_value=""):
                    btl.main()
        mock_get.assert_not_called()
        data = json.loads(output_path.read_text())
        assert data["lines"][data["trips"]["T1"][1]] == "3"

    def test_gtfs_zip_missing_file_exits(self, tmp_path):
        with patch("sys.argv", ["build_trip_lookup.py", "--gtfs-zip", str(tmp_path / "nope.zip"),
                                "--output", str(tmp_path / "out.json")]):
            with pytest.raises(SystemExit) as exc_info:
                btl.main()
        assert exc_info.value.code == 1
        assert not (tmp_path / "out.json").exists()

    @patch("build_trip_lookup.requests.get")
    def test_save_zip_keeps_download_and_builds_from_it(self, mock_get, tmp_path):
        payload = self._zip_bytes()
        mock_get.return_value = MagicMock(content=payload, raise_for_status=MagicMock())
        zip_path = tmp_path / "keep" / "sl.zip"
        zip_path.parent.mkdir()
        output_path = tmp_path / "trip_lookup.json"
        with patch("sys.argv", ["build_trip_lookup.py", "--key", "k", "--save-zip", str(zip_path),
                                "--output", str(output_path)]):
            btl.main()
        assert mock_get.call_count == 1
        assert zip_path.read_bytes() == payload          # reusable by the other scripts
        assert "T1" in json.loads(output_path.read_text())["trips"]
        # ...and a second build from it needs no download at all
        mock_get.reset_mock()
        with patch("sys.argv", ["build_trip_lookup.py", "--gtfs-zip", str(zip_path),
                                "--output", str(tmp_path / "again.json")]):
            btl.main()
        mock_get.assert_not_called()
        assert (tmp_path / "again.json").read_text() == output_path.read_text()

    def test_gtfs_zip_and_save_zip_are_mutually_exclusive(self, tmp_path):
        with patch("sys.argv", ["build_trip_lookup.py", "--gtfs-zip", "a.zip", "--save-zip", "b.zip"]):
            with pytest.raises(SystemExit) as exc_info:
                btl.main()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Helpers: generate trip_lookup.json files for validation tests
# ---------------------------------------------------------------------------

def _make_trip_lookup_data(num_trips=80_001, lines=None, routes=None, headsigns=None):
    """Generate a valid trip_lookup.json dict.

    Uses realistic-length trip IDs (like real GTFS data) so that 80k+ trips
    produce a file >2MB, matching the MIN_FILE_SIZE requirement.

    Args:
        num_trips: number of trips to generate
        lines: list of line names (default includes required line "80")
        routes: list of route ids (default auto-generated)
        headsigns: list of headsigns (default auto-generated)

    Returns:
        dict with routes, lines, headsigns, trips
    """
    if lines is None:
        lines = ["80", "1", "3", "42", "14"]
    if routes is None:
        routes = [f"R{i}" for i in range(len(lines))]
    if headsigns is None:
        headsigns = ["Centralen", "Södersjukhuset", "Fruängen", "Norsborg", "Djurgården"]

    trips = {}
    for i in range(num_trips):
        # Use realistic-length trip IDs (real GTFS IDs are ~20+ chars)
        trips[f"SL_TRIP_{i:06d}_2026"] = [
            i % len(routes),       # routeIdx
            i % len(lines),        # lineIdx
            700,                   # routeType
            i % len(headsigns),    # headsignIdx
        ]

    return {
        "routes": routes,
        "lines": lines,
        "headsigns": headsigns,
        "trips": trips,
    }


def _write_trip_lookup(path, data=None, **kwargs):
    """Write a trip_lookup.json file. Returns the file path.

    If data is None, generates valid data using _make_trip_lookup_data(**kwargs).
    """
    if data is None:
        data = _make_trip_lookup_data(**kwargs)
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    return path


# ---------------------------------------------------------------------------
# validate_trip_lookup: valid file passes
# ---------------------------------------------------------------------------

class TestValidateTripLookupValid:
    """A well-formed trip_lookup.json should pass validation."""

    def test_valid_file_passes(self, tmp_path):
        """A file with >80k trips, required lines, and >2MB should pass."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is True
        assert errors == []

    def test_valid_file_returns_no_errors(self, tmp_path):
        """The errors list should be empty for a valid file."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        _, errors = btl.validate_trip_lookup(path)
        assert len(errors) == 0

    def test_valid_with_old_file_same_size(self, tmp_path):
        """When old_path exists and new file is similar size, validation passes."""
        old_path = _write_trip_lookup(str(tmp_path / "old.json"))
        new_path = _write_trip_lookup(str(tmp_path / "new.json"))
        ok, errors = btl.validate_trip_lookup(new_path, old_path=old_path)
        assert ok is True
        assert errors == []


# ---------------------------------------------------------------------------
# validate_trip_lookup: trip count check
# ---------------------------------------------------------------------------

class TestValidateTripCount:
    """Validation should fail when trip count is below MIN_TRIPS."""

    def test_too_few_trips_fails(self, tmp_path):
        """A file with fewer than 50,000 trips should fail."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), num_trips=100)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert any("Too few trips" in e for e in errors)

    def test_exactly_min_trips_passes(self, tmp_path):
        """Exactly 50,000 trips should pass (boundary value)."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), num_trips=50_000)
        ok, errors = btl.validate_trip_lookup(path)
        # Should not have a trip-count error (may fail on file size though)
        assert not any("Too few trips" in e for e in errors)

    def test_normal_daily_variation_passes(self, tmp_path):
        """The absolute floor must sit well below a normal SL feed.

        Regression for 2026-09-10: the floor was 80,000 while the feed had been
        running 80.7k-84.4k for three weeks, so a 2% dip to 79,161 blocked the
        daily update. A normal day must clear the floor with room to spare.
        """
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), num_trips=79_161)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is True
        assert errors == []

    def test_zero_trips_fails(self, tmp_path):
        """A file with zero trips should fail."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), num_trips=0)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert any("Too few trips" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_trip_lookup: required lines check
# ---------------------------------------------------------------------------

class TestValidateRequiredLines:
    """Validation should fail when required lines are missing."""

    def test_missing_required_line_fails(self, tmp_path):
        """If line '80' is not in the lines array, validation should fail."""
        path = _write_trip_lookup(
            str(tmp_path / "trip_lookup.json"),
            lines=["1", "3", "42", "14", "7"],  # No "80"
        )
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert any("Required line '80' missing" in e for e in errors)

    def test_required_line_present_passes(self, tmp_path):
        """If line '80' is present, no line-related error should appear."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        _, errors = btl.validate_trip_lookup(path)
        assert not any("Required line" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_trip_lookup: file size check
# ---------------------------------------------------------------------------

class TestValidateFileSize:
    """Validation should fail when file is too small."""

    def test_file_too_small_fails(self, tmp_path):
        """A file under 1.5MB should fail."""
        path = str(tmp_path / "trip_lookup.json")
        # Write a tiny valid JSON
        data = _make_trip_lookup_data(num_trips=10)
        _write_trip_lookup(path, data=data)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert any("File too small" in e for e in errors)

    def test_large_enough_file_passes(self, tmp_path):
        """A file over 1.5MB should not trigger the size error."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        _, errors = btl.validate_trip_lookup(path)
        assert not any("File too small" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_trip_lookup: size ratio check (new vs old)
# ---------------------------------------------------------------------------

class TestValidateSizeRatio:
    """Validation should fail when new file is too large or too small vs old."""

    def test_new_file_too_large_vs_old_fails(self, tmp_path):
        """New file >2x the old file should fail."""
        old_path = _write_trip_lookup(str(tmp_path / "old.json"), num_trips=80_001)
        # Write a much larger new file (>2x trips = >2x size)
        new_path = _write_trip_lookup(str(tmp_path / "new.json"), num_trips=200_000)
        ok, errors = btl.validate_trip_lookup(new_path, old_path=old_path)
        assert ok is False
        assert any("New file too large" in e for e in errors)

    def test_no_old_file_skips_size_comparison(self, tmp_path):
        """When old_path is None, size ratio check should be skipped."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        ok, errors = btl.validate_trip_lookup(path, old_path=None)
        assert not any("New file too large" in e for e in errors)

    def test_old_file_does_not_exist_skips_comparison(self, tmp_path):
        """When old_path points to a nonexistent file, skip size comparison."""
        new_path = _write_trip_lookup(str(tmp_path / "new.json"))
        ok, errors = btl.validate_trip_lookup(new_path, old_path=str(tmp_path / "nonexistent.json"))
        assert not any("New file too large" in e for e in errors)

    def test_new_file_slightly_larger_passes(self, tmp_path):
        """New file <2x old file should pass size ratio check."""
        old_path = _write_trip_lookup(str(tmp_path / "old.json"), num_trips=80_001)
        # ~1.3x the trips -> ~1.3x the file size
        new_path = _write_trip_lookup(str(tmp_path / "new.json"), num_trips=105_000)
        _, errors = btl.validate_trip_lookup(new_path, old_path=old_path)
        assert not any("New file too large" in e for e in errors)

    def test_new_file_far_smaller_vs_old_fails(self, tmp_path):
        """A collapse in feed size should be caught, not silently accepted.

        Regression for 2026-08-18: SL's feed dropped from ~132k trips to ~104k
        overnight and validation waved it through, because only growth was
        guarded. A file well under min_size_ratio of the live one must fail.
        """
        old_path = _write_trip_lookup(str(tmp_path / "old.json"), num_trips=80_001)
        new_path = _write_trip_lookup(str(tmp_path / "new.json"), num_trips=50_001)
        ok, errors = btl.validate_trip_lookup(new_path, old_path=old_path)
        assert ok is False
        assert any("New file too small" in e for e in errors)

    def test_normal_daily_shrink_passes(self, tmp_path):
        """Ordinary day-to-day shrink stays well inside min_size_ratio."""
        old_path = _write_trip_lookup(str(tmp_path / "old.json"), num_trips=81_092)
        new_path = _write_trip_lookup(str(tmp_path / "new.json"), num_trips=79_161)
        ok, errors = btl.validate_trip_lookup(new_path, old_path=old_path)
        assert ok is True
        assert errors == []

    def test_no_old_file_skips_shrink_comparison(self, tmp_path):
        """With no old file to compare against, the shrink check is skipped."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        _, errors = btl.validate_trip_lookup(path, old_path=None)
        assert not any("New file too small" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_trip_lookup: invalid JSON
# ---------------------------------------------------------------------------

class TestValidateInvalidJson:
    """Validation should fail gracefully on invalid JSON files."""

    def test_invalid_json_fails(self, tmp_path):
        """A file with invalid JSON should return an error."""
        path = str(tmp_path / "trip_lookup.json")
        with open(path, "w") as f:
            f.write("{broken json content!!!")
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert len(errors) == 1
        assert "Cannot read/parse file" in errors[0]

    def test_nonexistent_file_fails(self, tmp_path):
        """A path to a nonexistent file should return an error."""
        ok, errors = btl.validate_trip_lookup(str(tmp_path / "missing.json"))
        assert ok is False
        assert len(errors) == 1
        assert "Cannot read/parse file" in errors[0]

    def test_empty_file_fails(self, tmp_path):
        """An empty file is not valid JSON."""
        path = str(tmp_path / "trip_lookup.json")
        with open(path, "w") as f:
            f.write("")
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert "Cannot read/parse file" in errors[0]


# ---------------------------------------------------------------------------
# validate_trip_lookup: missing keys
# ---------------------------------------------------------------------------

class TestValidateMissingKeys:
    """Validation should fail when required top-level keys are missing."""

    @pytest.mark.parametrize("missing_key", ["routes", "lines", "headsigns", "trips"])
    def test_missing_single_key_fails(self, tmp_path, missing_key):
        """Each of the four required keys, when missing, should cause failure."""
        data = _make_trip_lookup_data(num_trips=100)
        del data[missing_key]
        path = str(tmp_path / "trip_lookup.json")
        _write_trip_lookup(path, data=data)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert any(f"Missing key: {missing_key}" in e for e in errors)

    def test_all_keys_missing_reports_all(self, tmp_path):
        """An empty object should report all four missing keys."""
        path = str(tmp_path / "trip_lookup.json")
        with open(path, "w") as f:
            json.dump({}, f)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        assert len(errors) == 4
        missing = {e.split(": ")[1] for e in errors}
        assert missing == {"routes", "lines", "headsigns", "trips"}

    def test_extra_keys_do_not_cause_failure(self, tmp_path):
        """Extra keys beyond the required four should not cause failure."""
        data = _make_trip_lookup_data()
        data["extra_key"] = "should be ignored"
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), data=data)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is True


# ---------------------------------------------------------------------------
# validate_trip_lookup: multiple errors
# ---------------------------------------------------------------------------

class TestValidateMultipleErrors:
    """Validation should collect all errors, not stop at the first."""

    def test_multiple_errors_collected(self, tmp_path):
        """A file with few trips AND missing required line should report both."""
        data = _make_trip_lookup_data(num_trips=10, lines=["1", "3"])
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), data=data)
        ok, errors = btl.validate_trip_lookup(path)
        assert ok is False
        # Should have at least: too few trips, missing line '80', file too small
        assert len(errors) >= 3
        assert any("Too few trips" in e for e in errors)
        assert any("Required line '80' missing" in e for e in errors)
        assert any("File too small" in e for e in errors)


# ---------------------------------------------------------------------------
# --validate CLI flag
# ---------------------------------------------------------------------------

class TestValidateCli:
    """Tests for the --validate CLI flag in main()."""

    def test_validate_flag_valid_file_exits_zero(self, tmp_path):
        """--validate with a valid file should exit with code 0."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        with patch("sys.argv", ["build_trip_lookup.py", "--validate", path]):
            with pytest.raises(SystemExit) as exc_info:
                btl.main()
            assert exc_info.value.code == 0

    def test_validate_flag_invalid_file_exits_one(self, tmp_path):
        """--validate with an invalid file should exit with code 1."""
        data = _make_trip_lookup_data(num_trips=10)
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"), data=data)
        with patch("sys.argv", ["build_trip_lookup.py", "--validate", path]):
            with pytest.raises(SystemExit) as exc_info:
                btl.main()
            assert exc_info.value.code == 1

    def test_validate_flag_with_old_file(self, tmp_path):
        """--validate with --old should compare sizes."""
        old_path = _write_trip_lookup(str(tmp_path / "old.json"))
        new_path = _write_trip_lookup(str(tmp_path / "new.json"))
        with patch("sys.argv", [
            "build_trip_lookup.py", "--validate", new_path, "--old", old_path
        ]):
            with pytest.raises(SystemExit) as exc_info:
                btl.main()
            assert exc_info.value.code == 0

    def test_validate_flag_does_not_download(self, tmp_path):
        """--validate should not trigger download_gtfs."""
        path = _write_trip_lookup(str(tmp_path / "trip_lookup.json"))
        with patch("build_trip_lookup.download_gtfs") as mock_dl:
            with patch("sys.argv", ["build_trip_lookup.py", "--validate", path]):
                with pytest.raises(SystemExit):
                    btl.main()
            mock_dl.assert_not_called()

    def test_validate_nonexistent_file_exits_one(self, tmp_path):
        """--validate with a missing file should exit with code 1."""
        with patch("sys.argv", ["build_trip_lookup.py", "--validate", str(tmp_path / "nope.json")]):
            with pytest.raises(SystemExit) as exc_info:
                btl.main()
            assert exc_info.value.code == 1
