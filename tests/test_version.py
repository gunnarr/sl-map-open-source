"""Tests for SERVER_START version computation.

P1 — The server version drives client behavior:
- Screensaver mode auto-reloads on version bump (via /api/version polling)
- Regular users see "Ny version tillgänglig" banner

If _compute_version() ignores map.html mtime, frontend-only changes (like a
new banner) would never trigger reload for screensaver users — they'd be
stuck on the old version forever. These tests pin the contract.
"""

import json
import os
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _compute_version() — direct unit tests
# ---------------------------------------------------------------------------

class TestComputeVersion:
    """Unit tests for the version computation helper."""

    def test_uses_max_mtime_of_server_and_map_html(self, tmp_path, monkeypatch):
        """When both server.py and map.html exist, returns the newer mtime."""
        import server

        fake_server = tmp_path / "server.py"
        fake_map = tmp_path / "map.html"
        fake_server.write_text("# server")
        fake_map.write_text("<html></html>")

        # Set server.py mtime to older, map.html to newer
        older = 1_700_000_000
        newer = 1_800_000_000
        os.utime(fake_server, (older, older))
        os.utime(fake_map, (newer, newer))

        # Patch __file__ so _compute_version() looks at our fake files
        monkeypatch.setattr(server, "__file__", str(fake_server))

        result = server._compute_version()
        assert result == str(newer)

    def test_picks_server_py_when_it_is_newer(self, tmp_path, monkeypatch):
        """If server.py is newer than map.html, server.py mtime wins."""
        import server

        fake_server = tmp_path / "server.py"
        fake_map = tmp_path / "map.html"
        fake_server.write_text("# server")
        fake_map.write_text("<html></html>")

        newer = 1_900_000_000
        older = 1_700_000_000
        os.utime(fake_server, (newer, newer))
        os.utime(fake_map, (older, older))

        monkeypatch.setattr(server, "__file__", str(fake_server))

        result = server._compute_version()
        assert result == str(newer)

    def test_uses_only_existing_file_when_map_html_missing(self, tmp_path, monkeypatch):
        """Robust to a missing map.html — falls back to server.py mtime."""
        import server

        fake_server = tmp_path / "server.py"
        fake_server.write_text("# server")
        # Note: no map.html

        ts = 1_750_000_000
        os.utime(fake_server, (ts, ts))

        monkeypatch.setattr(server, "__file__", str(fake_server))

        result = server._compute_version()
        assert result == str(ts)

    def test_returns_zero_when_both_files_missing(self, tmp_path, monkeypatch):
        """Hypothetical edge case — neither file exists. Defensive default."""
        import server

        # Point __file__ at a non-existent path
        ghost = tmp_path / "does_not_exist.py"
        monkeypatch.setattr(server, "__file__", str(ghost))

        result = server._compute_version()
        assert result == "0"

    def test_returns_string_type(self, tmp_path, monkeypatch):
        """Return value must be a string for JSON serialization."""
        import server

        fake_server = tmp_path / "server.py"
        fake_server.write_text("# server")
        os.utime(fake_server, (1_700_000_000, 1_700_000_000))

        monkeypatch.setattr(server, "__file__", str(fake_server))

        result = server._compute_version()
        assert isinstance(result, str)

    def test_returns_integer_seconds_not_float(self, tmp_path, monkeypatch):
        """Mtime is truncated to int — no decimals leak into the version string.

        Float mtimes would make different workers report different versions
        even with the same source file (filesystem precision varies).
        """
        import server

        fake_server = tmp_path / "server.py"
        fake_server.write_text("# server")
        # Set fractional mtime — should be truncated
        os.utime(fake_server, (1_700_000_000.987654, 1_700_000_000.987654))

        monkeypatch.setattr(server, "__file__", str(fake_server))

        result = server._compute_version()
        # No dot in the result; pure integer seconds
        assert "." not in result
        assert result == str(int(1_700_000_000.987654))

    def test_changes_when_map_html_is_touched(self, tmp_path, monkeypatch):
        """The whole point of including map.html: touching it bumps the version.

        This is the regression test for the original bug — map.html-only
        changes used to be invisible to /api/version.
        """
        import server

        fake_server = tmp_path / "server.py"
        fake_map = tmp_path / "map.html"
        fake_server.write_text("# server")
        fake_map.write_text("<html></html>")

        old_ts = 1_700_000_000
        os.utime(fake_server, (old_ts, old_ts))
        os.utime(fake_map, (old_ts, old_ts))

        monkeypatch.setattr(server, "__file__", str(fake_server))

        before = server._compute_version()

        # Simulate someone editing map.html — only its mtime changes
        new_ts = 1_700_000_500
        os.utime(fake_map, (new_ts, new_ts))

        after = server._compute_version()

        assert after != before
        assert after == str(new_ts)


# ---------------------------------------------------------------------------
# /api/version endpoint
# ---------------------------------------------------------------------------

class TestApiVersionEndpoint:
    """The /api/version endpoint exposes SERVER_START to the client."""

    def test_returns_server_start_value(self, client):
        """Endpoint returns the SERVER_START string under key 'v'."""
        import server

        resp = client.get("/api/version")
        assert resp.status_code == 200

        data = resp.get_json()
        assert data == {"v": server.SERVER_START}

    def test_response_is_json(self, client):
        """Content type must be application/json for client polling."""
        resp = client.get("/api/version")
        assert resp.content_type.startswith("application/json")

    def test_v_field_is_string(self, client):
        """The 'v' field is a string (not an int) — clients compare strings."""
        resp = client.get("/api/version")
        data = resp.get_json()

        assert isinstance(data["v"], str)

    def test_v_field_is_numeric_string(self, client):
        """The version is a numeric string (epoch seconds), parseable as int."""
        resp = client.get("/api/version")
        data = resp.get_json()

        # Should not raise
        assert int(data["v"]) >= 0

    def test_health_endpoint_uses_same_version(self, client):
        """/health and /api/version must agree — they both expose SERVER_START."""
        version_resp = client.get("/api/version").get_json()
        health_resp = client.get("/health").get_json()

        assert version_resp["v"] == health_resp["version"]


def test_site_config_and_extension_also_bump_the_version(tmp_path, monkeypatch):
    """A new tile key or a backend change in private_ext.py reloads screensavers too."""
    import os
    import server

    fake_server = tmp_path / "server.py"
    fake_server.write_text("# fake")
    os.utime(fake_server, (1_000_000, 1_000_000))
    monkeypatch.setattr(server, "__file__", str(fake_server))

    for i, name in enumerate(("site-config.js", "private_ext.py"), start=1):
        f = tmp_path / name
        f.write_text("// x")
        os.utime(f, (2_000_000 + i, 2_000_000 + i))
        assert server._compute_version() == str(2_000_000 + i)
