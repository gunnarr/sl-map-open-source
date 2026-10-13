"""VEHICLE_CACHE_SECONDS sets how often /api/vehicles may call Trafiklab.

Default 2 s (what production runs). Someone on a small Trafiklab key raises
it so the feed is shared longer; the quota then lasts accordingly.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import server

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _settings(env_value=None):
    env = {k: v for k, v in os.environ.items() if k != "VEHICLE_CACHE_SECONDS"}
    env["TRAFIKLAB_KEY"] = ""
    if env_value is not None:
        env["VEHICLE_CACHE_SECONDS"] = env_value
    code = ("import sys, json; sys.modules['private_ext'] = None; sys.path.insert(0, %r); import server; "
            "print(json.dumps([server.VEHICLE_CACHE_SECONDS, server.TRIP_UPDATES_CACHE_SECONDS]))" % str(PROJECT_ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=PROJECT_ROOT, env=env, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_default_is_two_seconds_and_trip_updates_twenty():
    assert _settings() == [2, 20]


def test_env_value_is_used():
    assert _settings("15") == [15, 20]


def test_trip_updates_never_refresh_faster_than_positions():
    assert _settings("60") == [60, 60]


def test_zero_or_negative_is_clamped_to_one_second():
    assert _settings("0") == [1, 20]


def test_fetch_uses_the_setting(monkeypatch):
    seen = {}

    def fake_fetch_with_lock(key, max_age, fn):
        seen[key] = max_age
        return {"timestamp": 1, "vehicles": []}

    monkeypatch.setattr(server, "VEHICLE_CACHE_SECONDS", 15)
    monkeypatch.setattr(server, "_fetch_with_lock", fake_fetch_with_lock)
    server._fetch_vehicle_positions()
    assert seen == {"vehicles": 15}


def test_cached_feed_is_reused_within_the_window(monkeypatch):
    """A 10 s old feed is still served from cache at VEHICLE_CACHE_SECONDS=15."""
    upstream = MagicMock(side_effect=AssertionError("must not refetch"))
    monkeypatch.setattr(server, "VEHICLE_CACHE_SECONDS", 15)
    monkeypatch.setattr(server, "_do_fetch_vehicle_positions", upstream)
    monkeypatch.setitem(server._cache, "vehicles",
                        {"data": {"timestamp": 42, "vehicles": []}, "time": time.time() - 10})
    assert server._fetch_vehicle_positions()["timestamp"] == 42
    upstream.assert_not_called()
