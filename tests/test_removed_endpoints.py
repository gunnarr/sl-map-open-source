"""Regression tests for removed API endpoints.

P1 — These endpoints were removed during the multi-region refactoring. They
should return 404 (not 500 or any other unexpected status). External consumers
or bookmarked URLs must get a clean 404 response.
"""

import pytest


# ---------------------------------------------------------------------------
# Removed SL-proprietary endpoints (removed 2026-04-03)
# ---------------------------------------------------------------------------

class TestRemovedEndpointsReturn404:
    """Endpoints removed during multi-region refactoring must return 404."""

    def test_api_lines_returns_404(self, client):
        """GET /api/lines was removed — should return 404."""
        resp = client.get("/api/lines")
        assert resp.status_code == 404

    def test_api_search_returns_404(self, client):
        """GET /api/search was removed — should return 404."""
        resp = client.get("/api/search?q=slussen")
        assert resp.status_code == 404

    def test_api_departures_returns_404(self, client):
        """GET /api/departures/<site_id> was removed — should return 404."""
        resp = client.get("/api/departures/9192")
        assert resp.status_code == 404

    def test_api_nearby_returns_404(self, client):
        """GET /api/nearby was removed — should return 404."""
        resp = client.get("/api/nearby?lat=59.33&lon=18.07")
        assert resp.status_code == 404

    def test_api_line_vehicles_returns_404(self, client):
        """GET /api/line/<line_name>/vehicles was removed — should return 404."""
        resp = client.get("/api/line/bus-1/vehicles")
        assert resp.status_code == 404


class TestRemovedEndpointsWrongMethod:
    """POST to removed GET-only paths returns 405 (Flask catch-all only
    accepts GET). This confirms the dedicated route is truly gone — the
    response comes from the static file handler, not a leftover route."""

    def test_api_lines_post_returns_405(self, client):
        """POST /api/lines hits the static catch-all which is GET-only."""
        resp = client.post("/api/lines")
        assert resp.status_code == 405

    def test_api_search_post_returns_405(self, client):
        """POST /api/search hits the static catch-all which is GET-only."""
        resp = client.post("/api/search")
        assert resp.status_code == 405
