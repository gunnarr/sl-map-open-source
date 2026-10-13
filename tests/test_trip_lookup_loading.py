"""Tests for _load_trip_lookup() file resolution and the .bak fallback.

P1 — trip_lookup.json maps GTFS trip_id → route/line/headsign. Without it,
every vehicle on the map loses its line label and destination ("Buss 3 mot
Södersjukhuset" becomes a dot with no info). The file is untracked in git and
regenerated daily on the server by update-trip-lookup.sh; the .bak fallback
covers the deploy window where neither the region copy nor the BASE_DIR copy
exists yet. These tests guard the resolution order and the no-crash failure
path.

_load_trip_lookup() mutates module globals (_trip_lookup, _routes, _lines,
_headsigns) and also reads server.REGION_DIR / server.BASE_DIR. Every test
here saves and restores those so it can't pollute other test files.
"""

import json
from contextlib import contextmanager

import pytest


# A minimal but valid compact-format lookup (the real file is 3.4 MB).
SAMPLE_LOOKUP = {
    "routes": ["9011001000100000000", "9011001000300000000"],
    "lines": ["1", "3"],
    "headsigns": ["Stora Essingen", "Karolinska"],
    "trips": {
        "trip-aaa": [0, 0, 700, 0],   # route[0], line "1", BUS, headsign[0]
        "trip-bbb": [1, 1, 401, 1],   # route[1], line "3", METRO, headsign[1]
    },
}


@contextmanager
def _isolated_lookup(monkeypatch, tmp_path):
    """Point both REGION_DIR and BASE_DIR at empty tmp dirs and restore the
    four mutated globals afterwards.

    REGION_DIR is computed once at import time from the *real* repo root, so it
    still resolves to regions/sl (which has no trip_lookup.json). We override it
    with a guaranteed-empty path so the real region copy can never leak in and
    make these tests order/environment-dependent.
    """
    import server

    saved = {
        "REGION_DIR": server.REGION_DIR,
        "BASE_DIR": server.BASE_DIR,
        "_trip_lookup": server._trip_lookup,
        "_routes": server._routes,
        "_lines": server._lines,
        "_headsigns": server._headsigns,
    }
    region_dir = tmp_path / "regions" / "sl"
    region_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "REGION_DIR", region_dir)
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    try:
        yield server
    finally:
        server.REGION_DIR = saved["REGION_DIR"]
        server.BASE_DIR = saved["BASE_DIR"]
        server._trip_lookup = saved["_trip_lookup"]
        server._routes = saved["_routes"]
        server._lines = saved["_lines"]
        server._headsigns = saved["_headsigns"]


# ---------------------------------------------------------------------------
# .bak fallback (the change under test)
# ---------------------------------------------------------------------------

class TestBakFallback:
    """When neither REGION_DIR/trip_lookup.json nor BASE_DIR/trip_lookup.json
    exists, _load_trip_lookup() must fall back to BASE_DIR/trip_lookup.json.bak.
    """

    def test_loads_from_bak_when_primaries_missing(self, monkeypatch, tmp_path):
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            # Only the .bak exists.
            (tmp_path / "trip_lookup.json.bak").write_text(
                json.dumps(SAMPLE_LOOKUP), encoding="utf-8"
            )
            assert not (tmp_path / "trip_lookup.json").exists()

            server._load_trip_lookup()

            assert server._trip_lookup == SAMPLE_LOOKUP["trips"]
            assert server._routes == SAMPLE_LOOKUP["routes"]
            assert server._lines == SAMPLE_LOOKUP["lines"]
            assert server._headsigns == SAMPLE_LOOKUP["headsigns"]

    def test_bak_lookup_resolves_a_real_trip(self, monkeypatch, tmp_path):
        """Sanity: data loaded from .bak is usable for line resolution —
        trip-bbb → line '3', METRO, headsign 'Karolinska'."""
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            (tmp_path / "trip_lookup.json.bak").write_text(
                json.dumps(SAMPLE_LOOKUP), encoding="utf-8"
            )
            server._load_trip_lookup()

            info = server._trip_lookup["trip-bbb"]
            assert server._lines[info[1]] == "3"
            assert info[2] == 401  # METRO
            assert server._headsigns[info[3]] == "Karolinska"

    def test_emits_warning_when_using_bak(self, monkeypatch, tmp_path, capsys):
        """The deploy-window fallback should be visible in the logs."""
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            (tmp_path / "trip_lookup.json.bak").write_text(
                json.dumps(SAMPLE_LOOKUP), encoding="utf-8"
            )
            server._load_trip_lookup()

        out = capsys.readouterr().out
        assert "trip_lookup.json.bak" in out


# ---------------------------------------------------------------------------
# Resolution precedence — .bak must NOT win when a primary copy exists
# ---------------------------------------------------------------------------

class TestResolutionPrecedence:
    """The .bak is a last resort. A real trip_lookup.json (region or BASE_DIR)
    must take priority so a stale backup never shadows fresh data."""

    def test_basedir_copy_beats_bak(self, monkeypatch, tmp_path):
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            primary = dict(SAMPLE_LOOKUP)
            primary["lines"] = ["PRIMARY"]
            stale = dict(SAMPLE_LOOKUP)
            stale["lines"] = ["STALE_BAK"]

            (tmp_path / "trip_lookup.json").write_text(
                json.dumps(primary), encoding="utf-8"
            )
            (tmp_path / "trip_lookup.json.bak").write_text(
                json.dumps(stale), encoding="utf-8"
            )

            server._load_trip_lookup()
            assert server._lines == ["PRIMARY"]

    def test_region_copy_beats_basedir_and_bak(self, monkeypatch, tmp_path):
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            region = dict(SAMPLE_LOOKUP)
            region["lines"] = ["REGION"]
            basedir = dict(SAMPLE_LOOKUP)
            basedir["lines"] = ["BASEDIR"]
            stale = dict(SAMPLE_LOOKUP)
            stale["lines"] = ["STALE_BAK"]

            (server.REGION_DIR / "trip_lookup.json").write_text(
                json.dumps(region), encoding="utf-8"
            )
            (tmp_path / "trip_lookup.json").write_text(
                json.dumps(basedir), encoding="utf-8"
            )
            (tmp_path / "trip_lookup.json.bak").write_text(
                json.dumps(stale), encoding="utf-8"
            )

            server._load_trip_lookup()
            assert server._lines == ["REGION"]


# ---------------------------------------------------------------------------
# Missing-everything and degenerate inputs — must not crash
# ---------------------------------------------------------------------------

class TestMissingAndEmpty:
    """No file anywhere, or a structurally-empty file. The server must come up
    either way; vehicles simply lose their line info (handled elsewhere)."""

    def test_no_files_anywhere_warns_and_leaves_empty(self, monkeypatch, tmp_path, capsys):
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            # Pre-seed globals with junk to prove the warning path doesn't
            # overwrite them with anything but also doesn't crash.
            server._trip_lookup = {"sentinel": [0, 0, 700, -1]}
            server._routes = ["sentinel"]
            server._lines = ["sentinel"]
            server._headsigns = ["sentinel"]

            server._load_trip_lookup()  # must not raise

            # Globals are left untouched on the missing path (no assignment).
            assert server._trip_lookup == {"sentinel": [0, 0, 700, -1]}

        out = capsys.readouterr().out
        assert "trip_lookup.json not found" in out

    def test_empty_compact_lookup_loads_cleanly(self, monkeypatch, tmp_path):
        """A valid file with empty arrays/dict is acceptable — 0 mappings."""
        empty = {"routes": [], "lines": [], "headsigns": [], "trips": {}}
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            (tmp_path / "trip_lookup.json").write_text(
                json.dumps(empty), encoding="utf-8"
            )
            server._load_trip_lookup()

            assert server._trip_lookup == {}
            assert server._routes == []
            assert server._lines == []
            assert server._headsigns == []

    def test_headsigns_key_optional(self, monkeypatch, tmp_path):
        """Older files predate the headsigns array — raw.get('headsigns', [])
        must default to [] rather than KeyError."""
        no_headsigns = {
            "routes": ["r0"],
            "lines": ["1"],
            "trips": {"trip-aaa": [0, 0, 700, -1]},
        }
        with _isolated_lookup(monkeypatch, tmp_path) as server:
            (tmp_path / "trip_lookup.json").write_text(
                json.dumps(no_headsigns), encoding="utf-8"
            )
            server._load_trip_lookup()

            assert server._headsigns == []
            assert server._lines == ["1"]
