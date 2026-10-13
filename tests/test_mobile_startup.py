"""Regression tests for the mobile map startup waterfall."""

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _map_source():
    return (PROJECT_ROOT / "map.html").read_text(encoding="utf-8")


def test_vehicle_rendering_yields_between_batches():
    """A cold full feed must not monopolize Safari's main thread."""
    html = _map_source()
    assert "async function updateVehicles(vehicles)" in html
    assert "processed % 50 === 0" in html
    assert "await yieldToBrowser()" in html
    assert "await updateVehicles(vehicles)" in html


def test_offscreen_vehicle_markers_are_not_mounted_or_animated():
    """The full feed may stay in memory, but offscreen vehicles need no DOM."""
    html = _map_source()
    assert "map.getBounds().pad(0.35)" in html
    assert "renderBounds.contains([v.lat, v.lon])" in html
    assert "nearbyVehicles.concat(otherVehicles)" in html
    assert "map.on('moveend', applyLayerVisibility)" in html
    assert "if (map.hasLayer(marker))" in html
    assert "marker.setLatLng([toLat, toLng])" in html


def test_routes_start_before_first_vehicle_render_finishes():
    """Route requests should not sit behind creation of every vehicle marker."""
    html = _map_source()
    init = html.split("async function init() {", 1)[1].split("\n  }", 1)[0]
    assert init.index("const firstVehicles = fetchVehicles(window.__initialVehicles)") < init.index(
        "loadMetroRoutes()"
    )
    assert init.index("loadMetroRoutes()") < init.index("await firstVehicles")


def test_initial_pageshow_does_not_duplicate_vehicle_request():
    """Only bfcache restoration, not normal startup, should call onVisible()."""
    html = _map_source()
    pageshow = html.split("window.addEventListener('pageshow'", 1)[1].split(
        "\n  });", 1
    )[0]
    assert "e.persisted && !document.hidden" in pageshow


def test_tile_preloads_match_leaflets_no_cors_image_requests():
    """A CORS-mode mismatch makes browsers download every preload twice.

    The preloads are created from site-config.js (tiles.preload) by a small
    script in <head>; that script must not set crossorigin on them.
    """
    html = _map_source()
    head = html.split("</head>", 1)[0]
    boot = head.split('<script src="/site-config.js?v=3"></script>', 1)[1].split("</script>", 1)[0]
    assert "l.rel = 'preload'; l.as = 'image';" in boot
    assert "crossOrigin" not in boot
    assert "(t.preload || []).forEach" in boot


def test_tile_preloads_start_before_leaflet_and_analytics():
    """The tile warm-up runs right after site-config.js, ahead of the CSS/JS."""
    head = _map_source().split("</head>", 1)[0]
    assert head.index('<script src="/site-config.js?v=3"></script>') < head.index("l.rel = 'preload'")
    assert head.index("l.rel = 'preload'") < head.index('href="/leaflet.css"')
    assert head.index("l.rel = 'preload'") < head.index('src="/leaflet.js"')


def test_noncritical_work_is_lazy_or_async():
    """Analytics and unopened popup formatting must not delay map readiness."""
    html = _map_source()
    head = html.split("</head>", 1)[0]
    analytics = head.split("window.plausible=window.plausible||", 1)[1].split("</script>", 1)[0]
    # The analytics script is injected as an async script (from site-config.js),
    # never as a parser-blocking tag.
    assert "s.async = true;" in analytics
    assert "document.head.appendChild(s)" in analytics
    assert "marker.bindPopup(() =>" in html


def test_initial_vehicle_payload_starts_before_leaflet_is_ready():
    """Overlap the largest API download with the deferred Leaflet download."""
    html = _map_source()
    assert "window.__initialVehicles = fetch('/api/vehicles')" in html
    # ...and before the synchronous site-config.js, so that round trip never
    # holds it back.
    assert html.index("window.__initialVehicles = fetch('/api/vehicles')") < html.index(
        '<script src="/site-config.js?v=3"></script>'
    )
    assert "fetchVehicles(window.__initialVehicles)" in html
    assert "const resp = prefetched || await fetch(url)" in html
