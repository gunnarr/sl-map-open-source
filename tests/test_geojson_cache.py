"""Tests for GeoJSON memory cache (_get_geojson) and API endpoints.

P1 — GeoJSON endpoints serve track lines displayed on the map. If caching
breaks, every request re-reads files from disk. If the 404 path breaks,
users get 500 errors instead of a clean error message.
"""

import json

import pytest


SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[18.0, 59.3], [18.1, 59.4]],
            },
            "properties": {"line": "T10"},
        }
    ],
}


@pytest.fixture(autouse=True)
def _clear_geojson_cache():
    """Reset the module-level GeoJSON cache between tests."""
    import server

    original = server._geojson_cache.copy()
    server._geojson_cache.clear()
    yield
    server._geojson_cache.clear()
    server._geojson_cache.update(original)


@pytest.fixture()
def geojson_client(app, tmp_path):
    """Client with all three GeoJSON files written to BASE_DIR."""
    for name in ("metro_lines.geojson", "train_lines.geojson", "tram_lines.geojson"):
        (tmp_path / name).write_text(json.dumps(SAMPLE_GEOJSON), encoding="utf-8")
    return app.test_client()


# ---------------------------------------------------------------------------
# _get_geojson unit tests
# ---------------------------------------------------------------------------


class TestGetGeojson:
    """Direct tests for the _get_geojson helper function."""

    def test_loads_and_caches_file(self, app, tmp_path):
        import server

        (tmp_path / "test.geojson").write_text(
            json.dumps(SAMPLE_GEOJSON), encoding="utf-8"
        )
        result = server._get_geojson("test.geojson")

        assert result["type"] == "FeatureCollection"
        assert "test.geojson" in server._geojson_cache

    def test_returns_cached_on_second_call(self, app, tmp_path):
        import server

        path = tmp_path / "cached.geojson"
        path.write_text(json.dumps(SAMPLE_GEOJSON), encoding="utf-8")

        first = server._get_geojson("cached.geojson")
        # Delete the file — second call should still work from cache
        path.unlink()
        second = server._get_geojson("cached.geojson")

        assert first is second  # Same object, not re-read

    def test_returns_none_for_missing_file(self, app, tmp_path):
        import server

        result = server._get_geojson("nonexistent.geojson")
        assert result is None

    def test_missing_file_not_cached(self, app, tmp_path):
        import server

        server._get_geojson("missing.geojson")
        assert "missing.geojson" not in server._geojson_cache


# ---------------------------------------------------------------------------
# GeoJSON API endpoint tests
# ---------------------------------------------------------------------------


class TestGeoJsonEndpoints:
    """Verify all three GeoJSON endpoints return correct responses."""

    @pytest.mark.parametrize(
        "endpoint",
        ["/api/metro-lines", "/api/train-lines", "/api/tram-lines"],
    )
    def test_returns_geojson_data(self, geojson_client, endpoint):
        resp = geojson_client.get(endpoint)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1
        assert data["features"][0]["properties"]["line"] == "T10"

    @pytest.mark.parametrize(
        "endpoint,filename",
        [
            ("/api/metro-lines", "metro_lines.geojson"),
            ("/api/train-lines", "train_lines.geojson"),
            ("/api/tram-lines", "tram_lines.geojson"),
        ],
    )
    def test_returns_404_for_missing_file(self, client, endpoint, filename):
        resp = client.get(endpoint)
        assert resp.status_code == 404
        data = resp.get_json()
        assert "not found" in data["error"]
