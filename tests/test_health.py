"""Tests for GET /health endpoint and error rate tracking.

P0 — The health endpoint is used by monitoring/uptime systems to detect when
the service is degraded. If it gives false positives (200 when broken) or
false negatives (503 when healthy), alerting becomes useless. These tests
verify every check path and the underlying error tracking mechanism.
"""

import time
from collections import deque

import pytest


@pytest.fixture(autouse=True)
def _clear_cache_and_errors():
    """Reset vehicle cache and error timestamps between tests."""
    import server

    original_cache = server._cache.copy()
    original_errors = server._error_timestamps.copy()
    server._cache.clear()
    server._error_timestamps.clear()
    yield
    server._cache.clear()
    server._cache_locks.clear()
    server._cache.update(original_cache)
    server._error_timestamps.clear()
    server._error_timestamps.extend(original_errors)


# ---------------------------------------------------------------------------
# GET /health — no cached data (cold start)
# ---------------------------------------------------------------------------

class TestHealthNoCachedData:
    """When the server just started and has no vehicle data yet."""

    def test_returns_503_with_stale_freshness(self, client):
        """No API key and no cache means freshness is 'stale' — service not ready.

        With the active-fetch health check, the endpoint calls
        _fetch_vehicle_positions() which returns data without a timestamp
        when TRAFIKLAB_KEY is unset, resulting in a very large age.
        """
        resp = client.get("/health")
        assert resp.status_code == 503

        data = resp.get_json()
        assert data["status"] == "error"
        assert data["checks"]["freshness"]["status"] == "stale"
        assert "gammalt" in data["checks"]["freshness"]["message"]

    def test_returns_503_when_fetch_raises(self, client):
        """If _fetch_vehicle_positions() throws, health reports stale."""
        from unittest.mock import patch

        with patch("server._fetch_vehicle_positions", side_effect=RuntimeError("upstream down")):
            resp = client.get("/health")

        assert resp.status_code == 503
        data = resp.get_json()
        assert data["status"] == "error"
        assert data["checks"]["freshness"]["status"] == "stale"
        assert "Ingen fordonsdata tillgänglig" in data["checks"]["freshness"]["message"]

    def test_response_has_correct_structure(self, client):
        """All top-level keys present even when unhealthy."""
        resp = client.get("/health")
        data = resp.get_json()

        assert "status" in data
        assert "uptime" in data
        assert "version" in data
        assert "checks" in data
        assert isinstance(data["uptime"], int)
        assert isinstance(data["version"], str)
        assert isinstance(data["checks"], dict)

    def test_trip_lookup_missing_when_nothing_loaded(self, client, monkeypatch):
        """With no lookup data loaded in the worker, the check reports 'missing'."""
        import server
        monkeypatch.setattr(server, "_trip_lookup", {})

        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["trip_lookup"] == "missing"


# ---------------------------------------------------------------------------
# GET /health — active fetch (not passive cache read)
# ---------------------------------------------------------------------------

class TestHealthActiveFetch:
    """Health endpoint calls _fetch_vehicle_positions() instead of reading cache.

    This is the key behavioral property: each Gunicorn worker gets fresh data
    on health checks, even if it hasn't handled a recent /api/vehicles request.
    """

    def test_calls_fetch_vehicle_positions(self, client):
        """Health endpoint actively fetches data, not just reading _cache."""
        from unittest.mock import patch

        fresh_data = {
            "timestamp": time.time(),
            "vehicles": [{"id": "v1", "lat": 59.33, "lon": 18.07}],
        }

        with patch("server._fetch_vehicle_positions", return_value=fresh_data) as mock_fetch:
            resp = client.get("/health")
            mock_fetch.assert_called_once()

        data = resp.get_json()
        assert data["checks"]["freshness"]["status"] == "ok"
        assert data["checks"]["freshness"]["vehicle_count"] == 1

    def test_returns_fresh_data_even_with_empty_cache(self, client):
        """Even without prior cache, health can return 'ok' if upstream is live."""
        from unittest.mock import patch

        fresh_data = {
            "timestamp": time.time(),
            "vehicles": [
                {"id": "v1", "lat": 59.33, "lon": 18.07},
                {"id": "v2", "lat": 59.34, "lon": 18.08},
            ],
        }

        with patch("server._fetch_vehicle_positions", return_value=fresh_data):
            resp = client.get("/health")

        data = resp.get_json()
        assert data["checks"]["freshness"]["status"] == "ok"
        assert data["checks"]["freshness"]["vehicle_count"] == 2
        assert data["checks"]["freshness"]["age_seconds"] <= 2


# ---------------------------------------------------------------------------
# GET /health — fresh vehicle data
# ---------------------------------------------------------------------------

class TestHealthFreshData:
    """When vehicle cache contains recent data."""

    @pytest.fixture(autouse=True)
    def _inject_fresh_vehicles(self):
        import server

        server._cache["vehicles"] = {
            "data": {
                "timestamp": time.time(),
                "vehicles": [
                    {"id": "v1", "lat": 59.33, "lon": 18.07},
                    {"id": "v2", "lat": 59.34, "lon": 18.08},
                ],
            },
            "time": time.time(),
        }

    def test_returns_200_with_fresh_data(self, client, tmp_path, monkeypatch):
        """Fresh vehicles + loaded trip_lookup = all checks pass."""
        import server

        # Ensure loaded lookup data so that check also passes
        # (must not rely on a trip_lookup.json in the repo — CI has none)
        monkeypatch.setattr(server, "_trip_lookup", {"t": [0, 0, 700, -1]})

        resp = client.get("/health")
        assert resp.status_code == 200

        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["checks"]["freshness"]["status"] == "ok"
        assert data["checks"]["freshness"]["vehicle_count"] == 2
        assert data["checks"]["freshness"]["age_seconds"] <= 2
        assert data["checks"]["trip_lookup"] == "ok"
        assert data["checks"]["error_rate"]["status"] == "ok"

    def test_freshness_shows_vehicle_count(self, client):
        """Vehicle count is reported in the freshness check."""
        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["freshness"]["vehicle_count"] == 2

    def test_freshness_includes_threshold(self, client):
        """Threshold value is included in response for observability."""
        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["freshness"]["threshold_seconds"] == 30


# ---------------------------------------------------------------------------
# GET /health — stale vehicle data
# ---------------------------------------------------------------------------

class TestHealthStaleData:
    """When vehicle cache exists but data is old."""

    def test_returns_503_when_data_is_old(self, client):
        """Vehicle data older than 30 seconds is considered stale."""
        import server

        server._cache["vehicles"] = {
            "data": {
                "timestamp": time.time() - 60,  # 60 seconds old
                "vehicles": [{"id": "v1"}],
            },
            "time": time.time() - 60,
        }

        resp = client.get("/health")
        assert resp.status_code == 503

        data = resp.get_json()
        assert data["status"] == "error"
        assert data["checks"]["freshness"]["status"] == "stale"
        assert data["checks"]["freshness"]["age_seconds"] >= 59
        assert "gammalt" in data["checks"]["freshness"]["message"]

    def test_very_old_data_still_reports_age(self, client):
        """Even data that is hours old reports the correct age."""
        import server

        server._cache["vehicles"] = {
            "data": {
                "timestamp": time.time() - 3600,
                "vehicles": [],
            },
            "time": time.time() - 3600,
        }

        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["freshness"]["age_seconds"] >= 3599


# ---------------------------------------------------------------------------
# GET /health — elevated error rate
# ---------------------------------------------------------------------------

class TestHealthElevatedErrors:
    """When many errors have been recorded recently."""

    def test_returns_503_when_error_rate_exceeds_threshold(self, client, tmp_path, monkeypatch):
        """More than ERROR_THRESHOLD errors in 5 minutes triggers degraded status."""
        import server

        # Ensure loaded lookup so only error_rate fails
        monkeypatch.setattr(server, "_trip_lookup", {"t": [0, 0, 700, -1]})

        # Inject fresh vehicle data so only error_rate fails
        server._cache["vehicles"] = {
            "data": {
                "timestamp": time.time(),
                "vehicles": [{"id": "v1"}],
            },
            "time": time.time(),
        }

        # Record more errors than the threshold
        now = time.time()
        for i in range(server.ERROR_THRESHOLD + 5):
            server._error_timestamps.append(now - i)

        resp = client.get("/health")
        assert resp.status_code == 503

        data = resp.get_json()
        assert data["status"] == "error"
        assert data["checks"]["error_rate"]["status"] == "elevated"
        assert data["checks"]["error_rate"]["errors_last_5min"] >= server.ERROR_THRESHOLD + 1
        assert "fel senaste 5 minuterna" in data["checks"]["error_rate"]["message"]

    def test_error_rate_ok_when_below_threshold(self, client):
        """Fewer errors than the threshold is considered healthy."""
        import server

        now = time.time()
        for _ in range(server.ERROR_THRESHOLD - 1):
            server._error_timestamps.append(now)

        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["error_rate"]["status"] == "ok"
        assert data["checks"]["error_rate"]["errors_last_5min"] == server.ERROR_THRESHOLD - 1

    def test_error_rate_exactly_at_threshold_is_ok(self, client):
        """Exactly ERROR_THRESHOLD errors is still OK (threshold is >, not >=)."""
        import server

        now = time.time()
        for _ in range(server.ERROR_THRESHOLD):
            server._error_timestamps.append(now)

        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["error_rate"]["status"] == "ok"


# ---------------------------------------------------------------------------
# record_error() and get_error_rate()
# ---------------------------------------------------------------------------

class TestErrorRateTracking:
    """Unit tests for the error tracking mechanism."""

    def test_record_error_adds_timestamp(self):
        import server

        assert len(server._error_timestamps) == 0
        server.record_error()
        assert len(server._error_timestamps) == 1

    def test_get_error_rate_returns_count(self):
        import server

        server.record_error()
        server.record_error()
        server.record_error()

        assert server.get_error_rate() == 3

    def test_old_errors_are_pruned(self):
        """Errors older than ERROR_WINDOW are removed on get_error_rate()."""
        import server

        # Add old errors (older than the window)
        old_time = time.time() - server.ERROR_WINDOW - 10
        server._error_timestamps.append(old_time)
        server._error_timestamps.append(old_time - 5)

        # Add a recent error
        server.record_error()

        rate = server.get_error_rate()
        assert rate == 1  # Only the recent one remains
        assert len(server._error_timestamps) == 1

    def test_empty_error_list_returns_zero(self):
        import server

        assert server.get_error_rate() == 0

    def test_all_errors_within_window_are_counted(self):
        """Errors spread across the window are all counted."""
        import server

        now = time.time()
        for offset in [0, 60, 120, 180, 240]:
            server._error_timestamps.append(now - offset)

        assert server.get_error_rate() == 5

    def test_errors_exactly_at_boundary_are_pruned(self):
        """An error exactly at ERROR_WINDOW age should be pruned."""
        import server

        boundary = time.time() - server.ERROR_WINDOW
        server._error_timestamps.append(boundary - 0.001)  # Just outside
        server.record_error()  # Just now

        rate = server.get_error_rate()
        assert rate == 1


# ---------------------------------------------------------------------------
# GET /health — trip_lookup.json presence
# ---------------------------------------------------------------------------

class TestHealthTripLookup:
    """The trip_lookup check reflects whether lookup data is loaded in the
    worker — not whether the file exists on disk. The file is regenerated
    daily and may briefly be absent right after a deploy while workers keep
    serving from the copy loaded at startup."""

    def test_trip_lookup_ok_when_loaded(self, client, monkeypatch):
        """Loaded lookup data makes the check pass even without a file."""
        import server
        monkeypatch.setattr(server, "_trip_lookup", {"trip1": [0, 0, 700, -1]})

        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["trip_lookup"] == "ok"

    def test_trip_lookup_missing_when_not_loaded(self, client, monkeypatch):
        """Empty lookup reports 'missing' regardless of files on disk."""
        import server
        monkeypatch.setattr(server, "_trip_lookup", {})

        resp = client.get("/health")
        data = resp.get_json()

        assert data["checks"]["trip_lookup"] == "missing"


# ---------------------------------------------------------------------------
# GET /health — rate limiting exemption
# ---------------------------------------------------------------------------

class TestHealthNotRateLimited:
    """Health endpoint must be accessible for monitoring without rate limits."""

    def test_many_rapid_requests_all_succeed(self, client):
        """Monitoring systems poll frequently — all requests should work."""
        for _ in range(50):
            resp = client.get("/health")
            # Status can be 200 or 503 depending on checks,
            # but never 429 (rate limited)
            assert resp.status_code in (200, 503)

    def test_response_is_json(self, client):
        """Health endpoint always returns JSON (not HTML error page)."""
        resp = client.get("/health")
        assert resp.content_type.startswith("application/json")


# ---------------------------------------------------------------------------
# GET /health — version and uptime
# ---------------------------------------------------------------------------

class TestHealthMeta:
    """Top-level metadata fields in the health response."""

    def test_version_matches_server_start(self, client):
        import server

        resp = client.get("/health")
        data = resp.get_json()

        assert data["version"] == server.SERVER_START

    def test_uptime_is_non_negative(self, client):
        resp = client.get("/health")
        data = resp.get_json()

        assert data["uptime"] >= 0
