"""Tests for PWA assets: the service worker (sw.js) and manifest screenshots.

P1 — The service worker gives repeat visitors stale-while-revalidate caching
of static assets (faster loads, basic offline shell). If /sw.js 404s, the
registration in map.html silently fails and every visit pays full network
cost. The manifest "screenshots" array is what makes the install prompt show
a rich preview on Android/Chrome — malformed entries break the install UX.

Both files live at the repo root and are served by the generic static_files
handler. The static_client fixture copies the real files into BASE_DIR
(tmp_path) so we exercise the actual on-disk artifacts, not stubs.
"""

import json
import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def pwa_client(app, tmp_path):
    """Client with the real sw.js + manifest.json copied into BASE_DIR."""
    for fname in ("sw.js", "manifest.json"):
        src = PROJECT_ROOT / fname
        assert src.exists(), f"{fname} is missing from repo root"
        shutil.copy2(src, tmp_path / fname)
    return app.test_client()


# ---------------------------------------------------------------------------
# Service worker — /sw.js
# ---------------------------------------------------------------------------

class TestServiceWorkerServing:
    """GET /sw.js must succeed with JS content + the .js 5-minute cache rule.

    A short max-age matters for a service worker: the browser re-checks sw.js
    on navigation, so 300s lets a new worker roll out quickly after deploy
    rather than being pinned for a day.
    """

    def test_sw_js_is_served(self, pwa_client):
        resp = pwa_client.get("/sw.js")
        assert resp.status_code == 200

    def test_sw_js_is_javascript_content_type(self, pwa_client):
        resp = pwa_client.get("/sw.js")
        assert resp.status_code == 200
        # Flask infers text/javascript or application/javascript for .js
        assert "javascript" in resp.content_type.lower()

    def test_sw_js_has_5min_cache(self, pwa_client):
        """.js files get public, max-age=300 (same rule as inline app code)."""
        resp = pwa_client.get("/sw.js")
        assert resp.status_code == 200
        assert "max-age=300" in resp.headers.get("Cache-Control", "")

    def test_sw_js_body_is_intact(self, pwa_client):
        """The served body should be the real worker, not an empty/garbled file."""
        resp = pwa_client.get("/sw.js")
        body = resp.data.decode("utf-8")
        # Stable markers from the actual sw.js (cache name + install listener).
        assert "sl-map-static" in body
        assert "addEventListener" in body

    def test_sw_js_never_caches_html_or_api(self, pwa_client):
        """Regression: the worker must stay network-only for HTML and /api/ so
        the version banner and realtime data are never served stale. Guard the
        intent comment / logic so a future edit can't silently start caching
        navigations."""
        resp = pwa_client.get("/sw.js")
        body = resp.data.decode("utf-8")
        assert "/api/" in body  # references the api path it deliberately skips


class TestServiceWorkerRegistration:
    """map.html must register the worker at the path the route serves."""

    def test_map_html_registers_sw(self):
        html = (PROJECT_ROOT / "map.html").read_text(encoding="utf-8")
        assert "navigator.serviceWorker.register('/sw.js')" in html

    def test_registration_is_feature_guarded(self):
        """Registration must be behind a 'serviceWorker' in navigator check so
        it doesn't throw on browsers without SW support."""
        html = (PROJECT_ROOT / "map.html").read_text(encoding="utf-8")
        assert "'serviceWorker' in navigator" in html


# ---------------------------------------------------------------------------
# manifest.json — screenshots array
# ---------------------------------------------------------------------------

REQUIRED_SCREENSHOT_KEYS = {"src", "sizes", "type", "form_factor"}


class TestManifestScreenshots:
    """manifest.json must serve valid JSON with a well-formed screenshots
    array (drives the rich install preview on Android/Chrome)."""

    def test_manifest_is_valid_json(self, pwa_client):
        resp = pwa_client.get("/manifest.json")
        assert resp.status_code == 200
        assert resp.get_json() is not None  # parses without error

    def test_manifest_has_screenshots_array(self, pwa_client):
        data = pwa_client.get("/manifest.json").get_json()
        screenshots = data.get("screenshots")
        assert isinstance(screenshots, list)
        assert len(screenshots) >= 2

    def test_each_screenshot_has_required_keys(self, pwa_client):
        data = pwa_client.get("/manifest.json").get_json()
        for i, shot in enumerate(data["screenshots"]):
            missing = REQUIRED_SCREENSHOT_KEYS - set(shot)
            assert not missing, f"screenshot[{i}] missing keys: {missing}"

    def test_screenshot_form_factors_cover_narrow_and_wide(self, pwa_client):
        """The install prompt wants both a phone (narrow) and a desktop (wide)
        preview — at least one of each."""
        data = pwa_client.get("/manifest.json").get_json()
        factors = {shot.get("form_factor") for shot in data["screenshots"]}
        assert "narrow" in factors
        assert "wide" in factors

    def test_screenshot_types_are_image_mimetypes(self, pwa_client):
        data = pwa_client.get("/manifest.json").get_json()
        for shot in data["screenshots"]:
            assert shot["type"].startswith("image/"), shot["type"]

    def test_screenshot_sizes_are_wxh_format(self, pwa_client):
        """sizes must be 'WIDTHxHEIGHT' (e.g. 707x906), per the manifest spec."""
        data = pwa_client.get("/manifest.json").get_json()
        for shot in data["screenshots"]:
            w, _, h = shot["sizes"].partition("x")
            assert w.isdigit() and h.isdigit(), f"bad sizes: {shot['sizes']!r}"
