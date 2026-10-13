"""Delay data must never hold up vehicle positions.

Regression for the 2026-09-21 review, finding 4: TripUpdates was fetched
inline from the positions path. Its cache lasts 20s and the HTTP call has a
10s timeout, so a slow delay endpoint could hold a synchronous Gunicorn
worker — one of only three — while the positions it already had sat unsent.
"""

import threading
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _fresh_refresh_lock(monkeypatch):
    """Each test gets its own lock.

    A background refresher from an earlier test holds the module-level lock
    for as long as its stubbed upstream sleeps, which would silently make the
    next test's refresh a no-op and turn a real assertion green.
    """
    import server
    monkeypatch.setattr(server, "_trip_updates_refresh_lock", threading.Lock())


class TestDelaysDoNotBlockPositions:

    def test_cold_cache_returns_immediately(self, app):
        """With nothing cached, the caller gets {} now — not in 10 seconds."""
        import server
        server._cache.pop("trip_updates", None)

        started = threading.Event()

        def slow_upstream():
            started.set()
            time.sleep(5)
            return {"t1": 120}

        with patch.object(server, "_do_fetch_trip_updates", side_effect=slow_upstream):
            t0 = time.time()
            out = server._fetch_trip_updates()
            elapsed = time.time() - t0

        assert elapsed < 0.5, f"blocked for {elapsed:.2f}s on a slow upstream"
        assert out == {}
        assert started.wait(2), "no background refresh was started"

    def test_stale_data_is_served_while_refreshing(self, app):
        """Yesterday's delays beat no delays, and beat waiting."""
        import server
        server._set_cached("trip_updates", {"t9": 300})
        server._cache["trip_updates"]["time"] = time.time() - 3600  # long expired

        with patch.object(server, "_do_fetch_trip_updates",
                          side_effect=lambda: time.sleep(5) or {}):
            t0 = time.time()
            out = server._fetch_trip_updates()
            elapsed = time.time() - t0

        assert elapsed < 0.5
        assert out == {"t9": 300}

    def test_fresh_cache_does_not_spawn_a_refresh(self, app):
        import server
        server._set_cached("trip_updates", {"t1": 60})

        with patch.object(server, "_do_fetch_trip_updates") as upstream:
            out = server._fetch_trip_updates()

        assert out == {"t1": 60}
        upstream.assert_not_called()

    def test_only_one_background_refresh_at_a_time(self, app):
        """A burst of cold requests must not start a thread each."""
        import server
        server._cache.pop("trip_updates", None)
        calls = []
        release = threading.Event()

        def slow():
            calls.append(1)
            release.wait(3)
            return {}

        with patch.object(server, "_do_fetch_trip_updates", side_effect=slow):
            for _ in range(5):
                server._fetch_trip_updates()
            time.sleep(0.3)
            release.set()
            time.sleep(0.3)

        assert len(calls) == 1, f"{len(calls)} refreshers started, expected 1"

    def test_upstream_failure_does_not_reach_the_caller(self, app):
        import server
        server._cache.pop("trip_updates", None)

        with patch.object(server, "_do_fetch_trip_updates",
                          side_effect=RuntimeError("upstream nere")):
            out = server._fetch_trip_updates()
            time.sleep(0.3)

        assert out == {}
