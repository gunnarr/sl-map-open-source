"""Tests for static file serving — favicon, icons, and general static assets.

P1 — Favicon and PWA icons are loaded by every visitor's browser. A broken
favicon results in 404s in the console and missing branding. Correct
Cache-Control headers ensure browsers cache assets efficiently.
"""

import shutil
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def static_client(app, tmp_path):
    """Client with real static files copied into tmp_path (BASE_DIR)."""
    project_root = Path(__file__).resolve().parent.parent

    # Copy favicon and icon files into the test BASE_DIR
    for filename in ("favicon.ico", "icon-192.png", "icon-512.png"):
        src = project_root / filename
        if src.exists():
            shutil.copy2(src, tmp_path / filename)

    # Copy manifest.json (references icons)
    manifest = project_root / "manifest.json"
    if manifest.exists():
        shutil.copy2(manifest, tmp_path / "manifest.json")

    return app.test_client()


# ---------------------------------------------------------------------------
# Favicon serving
# ---------------------------------------------------------------------------

class TestFavicon:
    """Tests for /favicon.ico serving."""

    def test_favicon_ico_is_served(self, static_client):
        """GET /favicon.ico should return 200 with correct content type."""
        resp = static_client.get("/favicon.ico")
        assert resp.status_code == 200
        assert "image/" in resp.content_type  # image/x-icon or image/vnd.microsoft.icon

    def test_favicon_ico_has_cache_header(self, static_client):
        """favicon.ico should have 24h cache (86400s) like other static assets."""
        resp = static_client.get("/favicon.ico")
        assert resp.status_code == 200
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_favicon_ico_is_valid_ico(self, static_client):
        """The served favicon.ico should start with a valid ICO header."""
        resp = static_client.get("/favicon.ico")
        assert resp.status_code == 200
        data = resp.data
        # ICO files start with two zero bytes, then 01 00 (type = icon)
        assert len(data) >= 4
        assert data[0:4] == b"\x00\x00\x01\x00"

    def test_favicon_svg_no_longer_served(self, static_client, tmp_path):
        """favicon.svg should not exist (replaced by favicon.ico)."""
        # Ensure no favicon.svg in BASE_DIR
        svg_path = tmp_path / "favicon.svg"
        if svg_path.exists():
            svg_path.unlink()
        resp = static_client.get("/favicon.svg")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PWA Icons
# ---------------------------------------------------------------------------

class TestPwaIcons:
    """Tests for PWA icon files (icon-192.png, icon-512.png)."""

    def test_icon_192_is_served(self, static_client):
        """GET /icon-192.png should return 200 with image/png."""
        resp = static_client.get("/icon-192.png")
        assert resp.status_code == 200
        assert "image/png" in resp.content_type

    def test_icon_512_is_served(self, static_client):
        """GET /icon-512.png should return 200 with image/png."""
        resp = static_client.get("/icon-512.png")
        assert resp.status_code == 200
        assert "image/png" in resp.content_type

    def test_icon_192_has_cache_header(self, static_client):
        """Icon files should have 24h cache."""
        resp = static_client.get("/icon-192.png")
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_icon_512_has_cache_header(self, static_client):
        """Icon files should have 24h cache."""
        resp = static_client.get("/icon-512.png")
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_icon_192_is_valid_png(self, static_client):
        """icon-192.png should have a valid PNG header."""
        resp = static_client.get("/icon-192.png")
        assert resp.status_code == 200
        # PNG magic bytes
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_icon_512_is_valid_png(self, static_client):
        """icon-512.png should have a valid PNG header."""
        resp = static_client.get("/icon-512.png")
        assert resp.status_code == 200
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# manifest.json icon references
# ---------------------------------------------------------------------------

class TestManifestIcons:
    """Verify manifest.json references the correct icon files."""

    def test_manifest_references_icon_192(self, static_client):
        """manifest.json should reference icon-192.png."""
        resp = static_client.get("/manifest.json")
        assert resp.status_code == 200
        data = resp.get_json()
        icons = data.get("icons", [])
        srcs = [icon.get("src", "") for icon in icons]
        assert any("icon-192" in src for src in srcs), f"icon-192 not found in manifest icons: {srcs}"

    def test_manifest_references_icon_512(self, static_client):
        """manifest.json should reference icon-512.png."""
        resp = static_client.get("/manifest.json")
        assert resp.status_code == 200
        data = resp.get_json()
        icons = data.get("icons", [])
        srcs = [icon.get("src", "") for icon in icons]
        assert any("icon-512" in src for src in srcs), f"icon-512 not found in manifest icons: {srcs}"


# ---------------------------------------------------------------------------
# Static file security (regression tests)
# ---------------------------------------------------------------------------

class TestStaticFileSecurity:
    """Ensure static file serving blocks sensitive paths."""

    def test_dotfiles_blocked(self, static_client, tmp_path):
        """Dotfiles like .env should return 404."""
        (tmp_path / ".env").write_text("SECRET=bad")
        resp = static_client.get("/.env")
        assert resp.status_code == 404

    def test_deploy_dir_blocked(self, static_client, tmp_path):
        """deploy/ directory should be blocked."""
        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        (deploy_dir / "secrets.txt").write_text("secret")
        resp = static_client.get("/deploy/secrets.txt")
        assert resp.status_code == 404

    def test_disallowed_extension_blocked(self, static_client, tmp_path):
        """Files with non-allowed extensions (e.g. .py) should return 404."""
        (tmp_path / "test.py").write_text("import os")
        resp = static_client.get("/test.py")
        assert resp.status_code == 404

    def test_allowed_extensions_served(self, static_client, tmp_path):
        """Files with allowed extensions should be served."""
        (tmp_path / "style.css").write_text("body { color: red; }")
        resp = static_client.get("/style.css")
        assert resp.status_code == 200

    @pytest.mark.parametrize("name", ["greetings.txt", "sent_newsletters.txt", "notes.txt"])
    def test_private_text_files_never_served(self, static_client, tmp_path, name):
        """The contact form appends visitor messages (with e-mail addresses) to
        greetings.txt in BASE_DIR. In production nginx denies *.txt, but a
        self-hosted instance without that nginx rule must not leak it either."""
        (tmp_path / name).write_text("Anna <anna@example.com>: hej!")
        resp = static_client.get(f"/{name}")
        assert resp.status_code == 404
        assert b"anna@example.com" not in resp.data

    @pytest.mark.parametrize("name", ["robots.txt", "llms.txt", "0123456789abcdef0123456789abcdef.txt"])
    def test_public_text_files_still_served(self, static_client, tmp_path, name):
        """The text files that are public by design (crawlers, LLMs, the
        IndexNow key) stay reachable, mirroring the nginx allowlist."""
        (tmp_path / name).write_text("public")
        resp = static_client.get(f"/{name}")
        assert resp.status_code == 200
        assert resp.data == b"public"

    @pytest.mark.parametrize("name", ["installer.exe", "stasis.scr"])
    def test_windows_binaries_not_served(self, static_client, tmp_path, name):
        """Nothing links to Windows builds any more; don't serve stray ones."""
        (tmp_path / name).write_bytes(b"MZ")
        resp = static_client.get(f"/{name}")
        assert resp.status_code == 404


class TestClientIp:
    """X-Real-IP is only trusted from a local reverse proxy. Without that, a
    client could pick its own IP to dodge rate limits or reach /api/config."""

    def test_spoofed_header_from_remote_client_is_ignored(self, app):
        import server
        with app.test_request_context("/", headers={"X-Real-IP": "127.0.0.1"},
                                      environ_base={"REMOTE_ADDR": "203.0.113.7"}):
            assert server._get_real_ip() == "203.0.113.7"

    def test_header_trusted_from_local_proxy(self, app):
        import server
        with app.test_request_context("/", headers={"X-Real-IP": "198.51.100.4"},
                                      environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            assert server._get_real_ip() == "198.51.100.4"

    def test_no_header_falls_back_to_remote_addr(self, app):
        import server
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "198.51.100.9"}):
            assert server._get_real_ip() == "198.51.100.9"

    def test_api_config_not_reachable_with_spoofed_header(self, app):
        resp = app.test_client().get("/api/config", headers={"X-Real-IP": "127.0.0.1"},
                                     environ_base={"REMOTE_ADDR": "203.0.113.7"})
        assert resp.status_code == 403

    def test_trusted_proxies_configurable(self, app, monkeypatch):
        import server
        monkeypatch.setattr(server, "TRUSTED_PROXIES", {"10.0.0.2"})
        with app.test_request_context("/", headers={"X-Real-IP": "198.51.100.4"},
                                      environ_base={"REMOTE_ADDR": "10.0.0.2"}):
            assert server._get_real_ip() == "198.51.100.4"
        with app.test_request_context("/", headers={"X-Real-IP": "198.51.100.4"},
                                      environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            assert server._get_real_ip() == "127.0.0.1"


# ---------------------------------------------------------------------------
# Cache-Control headers by file type
# ---------------------------------------------------------------------------

class TestCacheHeaders:
    """Verify Cache-Control headers are set correctly per file type."""

    def test_html_gets_5min_cache(self, static_client, tmp_path):
        """HTML files should have 5-minute cache (300s)."""
        (tmp_path / "test.html").write_text("<html></html>")
        resp = static_client.get("/test.html")
        assert resp.status_code == 200
        assert "max-age=300" in resp.headers.get("Cache-Control", "")

    def test_css_gets_5min_cache(self, static_client, tmp_path):
        """CSS files should have 5-minute cache."""
        (tmp_path / "test.css").write_text("body{}")
        resp = static_client.get("/test.css")
        assert resp.status_code == 200
        assert "max-age=300" in resp.headers.get("Cache-Control", "")

    def test_js_gets_5min_cache(self, static_client, tmp_path):
        """JS files should have 5-minute cache."""
        (tmp_path / "test.js").write_text("console.log(1)")
        resp = static_client.get("/test.js")
        assert resp.status_code == 200
        assert "max-age=300" in resp.headers.get("Cache-Control", "")

    def test_png_gets_24h_cache(self, static_client, tmp_path):
        """PNG files should have 24h cache."""
        (tmp_path / "test.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        resp = static_client.get("/test.png")
        assert resp.status_code == 200
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_json_gets_24h_cache(self, static_client, tmp_path):
        """JSON files should have 24h cache."""
        (tmp_path / "test.json").write_text("{}")
        resp = static_client.get("/test.json")
        assert resp.status_code == 200
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_ico_gets_24h_cache(self, static_client, tmp_path):
        """ICO files should have 24h cache."""
        (tmp_path / "test.ico").write_bytes(b"\x00\x00\x01\x00")
        resp = static_client.get("/test.ico")
        assert resp.status_code == 200
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_webp_gets_24h_cache(self, static_client, tmp_path):
        """WebP files should have 24h cache, same as other images."""
        # Minimal WebP container header: "RIFF" + size + "WEBP"
        (tmp_path / "test.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        resp = static_client.get("/test.webp")
        assert resp.status_code == 200
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# WebP static serving (P2 — added 2026-06)
# ---------------------------------------------------------------------------

class TestWebpServing:
    """`.webp` is an allowed static extension served with the 24h image cache.

    WebP gives ~30% smaller images than PNG/JPEG; adding it lets the site ship
    illustrations/screenshots as WebP without 404s. It must behave exactly like
    the other image types (.png/.jpg): served, 24h cache, no Content-Disposition.
    """

    def test_webp_is_served(self, static_client, tmp_path):
        """A .webp file in BASE_DIR should be served with 200."""
        (tmp_path / "illustration.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        resp = static_client.get("/illustration.webp")
        assert resp.status_code == 200

    def test_webp_in_allowed_extensions(self):
        """Regression: .webp must be whitelisted in ALLOWED_STATIC_EXT."""
        import server
        assert ".webp" in server.ALLOWED_STATIC_EXT

    def test_webp_has_24h_cache(self, static_client, tmp_path):
        """WebP gets the same 24h immutable-ish cache as .png/.jpg."""
        (tmp_path / "shot.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        resp = static_client.get("/shot.webp")
        assert resp.status_code == 200
        assert "public, max-age=86400" in resp.headers.get("Cache-Control", "")

    def test_webp_not_treated_as_download(self, static_client, tmp_path):
        """Unlike .pkg, .webp must render inline, not as a forced attachment.

        Flask's send_from_directory sets Content-Disposition: inline by default;
        only the .pkg branch overrides it to attachment. WebP is an image and
        must display in the browser.
        """
        (tmp_path / "shot.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        resp = static_client.get("/shot.webp")
        assert resp.status_code == 200
        disposition = resp.headers.get("Content-Disposition", "inline")
        assert "attachment" not in disposition

    def test_webp_body_is_served_intact(self, static_client, tmp_path):
        """The served bytes match what was written (no transformation)."""
        payload = b"RIFF\x10\x00\x00\x00WEBPVP8 dummybytes"
        (tmp_path / "img.webp").write_bytes(payload)
        resp = static_client.get("/img.webp")
        assert resp.status_code == 200
        assert resp.data == payload
