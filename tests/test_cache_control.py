"""Tests for Cache-Control headers on GeoJSON API endpoints.

P1 — These endpoints serve data that changes infrequently (track GeoJSON).
Correct Cache-Control headers reduce unnecessary requests from browsers and
improve perceived performance for all users.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_geojson_cache():
    """Reset GeoJSON cache between tests to prevent cross-test leakage."""
    import server

    original = server._geojson_cache.copy()
    server._geojson_cache.clear()
    yield
    server._geojson_cache.clear()
    server._geojson_cache.update(original)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[18.0, 59.3], [18.1, 59.4]]},
            "properties": {"line": "1"},
        }
    ],
}


@pytest.fixture()
def geojson_client(app, tmp_path):
    """Client with GeoJSON files available in tmp_path (BASE_DIR)."""
    for name in ("metro_lines.geojson", "train_lines.geojson", "tram_lines.geojson"):
        (tmp_path / name).write_text(json.dumps(SAMPLE_GEOJSON), encoding="utf-8")
    return app.test_client()


# ---------------------------------------------------------------------------
# /api/metro-lines
# ---------------------------------------------------------------------------

class TestApiMetroLinesCacheControl:
    """Verify /api/metro-lines returns Cache-Control: max-age=3600."""

    def test_metro_lines_has_cache_control_3600(self, geojson_client):
        """GET /api/metro-lines should return max-age=3600."""
        resp = geojson_client.get("/api/metro-lines")
        assert resp.status_code == 200
        cache = resp.headers.get("Cache-Control", "")
        assert "max-age=3600" in cache

    def test_metro_lines_returns_geojson(self, geojson_client):
        """GET /api/metro-lines should return valid GeoJSON."""
        resp = geojson_client.get("/api/metro-lines")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) > 0

    def test_metro_lines_missing_file_returns_404(self, client):
        """GET /api/metro-lines without GeoJSON file should return 404."""
        resp = client.get("/api/metro-lines")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/train-lines
# ---------------------------------------------------------------------------

class TestApiTrainLinesCacheControl:
    """Verify /api/train-lines returns Cache-Control: max-age=3600."""

    def test_train_lines_has_cache_control_3600(self, geojson_client):
        """GET /api/train-lines should return max-age=3600."""
        resp = geojson_client.get("/api/train-lines")
        assert resp.status_code == 200
        cache = resp.headers.get("Cache-Control", "")
        assert "max-age=3600" in cache

    def test_train_lines_returns_geojson(self, geojson_client):
        """GET /api/train-lines should return valid GeoJSON."""
        resp = geojson_client.get("/api/train-lines")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"

    def test_train_lines_missing_file_returns_404(self, client):
        """GET /api/train-lines without GeoJSON file should return 404."""
        resp = client.get("/api/train-lines")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/tram-lines
# ---------------------------------------------------------------------------

class TestApiTramLinesCacheControl:
    """Verify /api/tram-lines returns Cache-Control: max-age=3600."""

    def test_tram_lines_has_cache_control_3600(self, geojson_client):
        """GET /api/tram-lines should return max-age=3600."""
        resp = geojson_client.get("/api/tram-lines")
        assert resp.status_code == 200
        cache = resp.headers.get("Cache-Control", "")
        assert "max-age=3600" in cache

    def test_tram_lines_returns_geojson(self, geojson_client):
        """GET /api/tram-lines should return valid GeoJSON."""
        resp = geojson_client.get("/api/tram-lines")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"

    def test_tram_lines_missing_file_returns_404(self, client):
        """GET /api/tram-lines without GeoJSON file should return 404."""
        resp = client.get("/api/tram-lines")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-cutting: all three GeoJSON endpoints use the same cache duration
# ---------------------------------------------------------------------------

class TestCacheControlConsistency:
    """All GeoJSON endpoints should use the same 1-hour cache duration."""

    def test_all_geojson_endpoints_share_same_max_age(self, geojson_client):
        """All three GeoJSON endpoints should have identical max-age=3600."""
        endpoints = [
            "/api/metro-lines",
            "/api/train-lines",
            "/api/tram-lines",
        ]
        for endpoint in endpoints:
            resp = geojson_client.get(endpoint)
            assert resp.status_code == 200, f"{endpoint} returned {resp.status_code}"
            cache = resp.headers.get("Cache-Control", "")
            assert "max-age=3600" in cache, (
                f"{endpoint} has Cache-Control: {cache!r}, expected max-age=3600"
            )
