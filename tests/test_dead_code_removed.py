"""Regression tests for dead-code removal (refactor 2026-04-29).

P2 — `_stations` global and `_load_stations()` were removed when `/api/search`
was deleted. This locks in that stations.json is no longer touched at startup
and that the symbols stay gone, so a future revert doesn't silently bring back
unused disk I/O on every Gunicorn worker boot.
"""


def test_load_stations_symbol_removed():
    """`_load_stations` must not exist on the server module."""
    import server

    assert not hasattr(server, "_load_stations")


def test_stations_global_removed():
    """`_stations` global must not exist on the server module."""
    import server

    assert not hasattr(server, "_stations")


def test_stations_json_not_referenced_in_source():
    """server.py must not reference stations.json at all."""
    from pathlib import Path
    import server

    src = Path(server.__file__).read_text(encoding="utf-8")
    assert "stations.json" not in src
    assert "_load_stations" not in src
