"""The core server must run on its own, without the optional private_ext module.

The open-source export ships server.py without private_ext.py. These tests
start a fresh interpreter in which `import private_ext` fails the same way it
does there, and check what a visitor of such a deployment gets.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PROBE = r"""
import json, sys
sys.modules["private_ext"] = None  # import private_ext -> ModuleNotFoundError
sys.path.insert(0, ROOT)
import server
server.limiter.enabled = False
c = server.app.test_client()
out = {
    "extensions": len(server.EXTENSIONS),
    "visitors": [c.get("/api/visitors").status_code, c.get("/api/visitors").get_json()],
    "version": c.get("/api/version").status_code,
    "sitemap": c.get("/sitemap.xml").get_data(as_text=True),
    "status": {p: c.get(p).status_code for p in (
        "/nyheter", "/feed.xml", "/lgtv", "/api/stats", "/api/greetings",
        "/api/screensaver-stats")},
    "post": {p: c.post(p, json={}).status_code for p in (
        "/api/greeting", "/api/subscribe", "/api/screensaver-diag",
        "/api/send-screensaver-instructions")},
}
print(json.dumps(out))
"""


def _run_without_extension():
    code = _PROBE.replace("ROOT", repr(str(PROJECT_ROOT)))
    env = {k: v for k, v in os.environ.items() if k not in ("SITE_DOMAIN", "CORS_ORIGINS")}
    env["TRAFIKLAB_KEY"] = ""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=PROJECT_ROOT, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_core_runs_without_private_ext():
    out = _run_without_extension()
    assert out["extensions"] == 0
    assert out["version"] == 200

    # The map footer hides the figure when the count is 0.
    assert out["visitors"] == [200, {"visitors": 0}]

    # Only the map in the sitemap — no links to pages that don't exist.
    sitemap = out["sitemap"]
    assert sitemap.count("<url>") == 1
    assert "/nyheter" not in sitemap and "about.html" not in sitemap

    # Private routes are simply absent.
    assert all(code == 404 for code in out["status"].values()), out["status"]
    assert all(code in (404, 405) for code in out["post"].values()), out["post"]


def test_an_error_inside_the_extension_is_not_swallowed():
    """Only a *missing* private_ext is tolerated; a broken one must fail loudly."""
    src = (PROJECT_ROOT / "server.py").read_text(encoding="utf-8")
    block = src.split("try:\n    import private_ext", 1)[1].split("_load_trip_lookup()", 1)[0]
    assert "except ModuleNotFoundError as _exc:" in block
    assert 'if _exc.name != "private_ext":\n        raise' in block
