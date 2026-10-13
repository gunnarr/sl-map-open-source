"""site-config.js holds every deployment-specific value map.html needs.

map.html is the same for every deployment: basemap tiles (and their keys),
analytics and the native-app banner come from site-config.js, which each
deployment edits. The checks for this deployment's actual values live in
tests/private/.

nginx serves map.html and *.js straight from disk in production, so this split
cannot be done with server-side templating; both files must simply exist.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAP_HTML = PROJECT_ROOT / "map.html"
SITE_CONFIG = PROJECT_ROOT / "site-config.js"


def _map():
    return MAP_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# map.html carries no deployment values
# ---------------------------------------------------------------------------

class TestMapHtmlIsNeutral:
    def test_no_tile_urls_or_keys(self):
        html = _map()
        assert "{z}/{x}/{y}" not in html.replace("tile.openstreetmap.org/{z}/{x}/{y}", "")
        assert "?key=" not in html and "api_key=" not in html

    def test_no_app_store_id(self):
        import re
        assert not re.search(r"app-id=\d", _map())

    def test_no_analytics_script_tag(self):
        assert "data-api=" not in _map()

    def test_no_static_tile_preloads(self):
        assert '<link rel="preload" as="image"' not in _map()

    def test_loads_site_config_synchronously_in_head(self):
        head = _map().split("</head>", 1)[0]
        tag = '<script src="/site-config.js?v=3"></script>'
        assert tag in head  # no async/defer: values must exist before use
        # ...before the theme/map code and before anything reads window.SITE
        assert head.index(tag) < head.index("window.SITE.analytics")
        assert head.index(tag) < head.index('src="/leaflet.js"')

    def test_plausible_stub_kept_for_analytics_off(self):
        """Event calls in the map code must stay no-ops without analytics."""
        html = _map()
        assert "window.plausible=window.plausible||function()" in html
        assert "plausible('Theme Toggle'" in html

    def test_app_banner_only_from_config_and_never_in_screensaver(self):
        head = _map().split("</head>", 1)[0]
        assert "S.appBanner && S.appBanner.appId && !screensaver" in head
        assert "apple-itunes-app" in head

    def test_links_to_deployment_pages_can_be_hidden_by_config(self):
        """Every link to a page outside the map carries data-page, so a
        deployment without those pages (SITE.pages) can hide it."""
        import re
        html = _map()
        body = html.split("<body", 1)[1]
        for href in ("about.html", "/nyheter", "/contact.html", "/screensaver.html", "/install.html"):
            links = re.findall(r'<a href="' + re.escape(href) + r'"[^>]*>', body)
            assert links, href
            assert all("data-page=" in a for a in links), href
        assert '<span data-page="credit">' in body
        head = html.split("</head>", 1)[0]
        assert "Array.isArray(S.pages)" in head
        for page in ("about", "news", "contact", "screensaver", "install", "credit"):
            assert f"'{page}'" in head.split("Array.isArray(S.pages)", 1)[1].split("document.write", 1)[0]

    def test_tile_layer_comes_from_site_config(self):
        html = _map()
        assert "window.SITE && window.SITE.tiles" in html
        assert "makeTileLayer(isDark)" in html
        assert "makeTileLayer(dark)" in html


# ---------------------------------------------------------------------------
# Served correctly
# ---------------------------------------------------------------------------

class TestServing:
    def test_flask_serves_site_config_as_js(self, client, app):
        import server
        shutil.copy(SITE_CONFIG, server.BASE_DIR / "site-config.js")
        resp = client.get("/site-config.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["Content-Type"]
        # Same short cache as map.html (and nginx's `expires 5m` for *.js).
        assert resp.headers["Cache-Control"] == "public, max-age=300"

    def test_service_worker_never_caches_site_config(self):
        sw = (PROJECT_ROOT / "sw.js").read_text(encoding="utf-8")
        assert "if (url.pathname === '/site-config.js') return;" in sw


# ---------------------------------------------------------------------------
# The config evaluates to the documented shape (needs node)
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")


def _evaluate(path):
    script = (
        "const fs=require('fs');const window={};"
        f"eval(fs.readFileSync({json.dumps(str(path))},'utf8'));"
        "process.stdout.write(JSON.stringify(window.SITE));"
    )
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(out.stdout)


def _check_shape(site):
    tiles = site["tiles"]
    assert tiles.get("light") or tiles.get("dark")
    for theme in ("light", "dark"):
        layer = tiles.get(theme)
        if layer is None:
            continue
        assert layer["url"].startswith("https://")
        assert "{z}" in layer["url"] and "{x}" in layer["url"] and "{y}" in layer["url"]
        assert layer["attribution"]
        assert isinstance(layer["maxZoom"], int)
        if "{s}" in layer["url"]:
            assert layer.get("subdomains")
    for coord in tiles.get("preload", []):
        assert len(coord) == 3 and all(isinstance(n, int) for n in coord)
    assert "analytics" in site and "appBanner" in site
    pages = site.get("pages")  # absent = every link shows
    if pages is not None:
        assert isinstance(pages, list)
        assert set(pages) <= {"about", "news", "contact", "screensaver", "install", "credit"}


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_site_config_shape():
    assert SITE_CONFIG.is_file()
    _check_shape(_evaluate(SITE_CONFIG))
