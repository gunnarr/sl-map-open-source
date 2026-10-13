"""Tests for the network-isolation guarantees in conftest.py.

P0 — not user-facing, but this is the guardrail that keeps the whole suite
honest. A regression here means tests silently call live APIs — real
Trafiklab quota and, on hosted CI runners, 5-10s per unmocked call (that once
took CI from ~3s to ~180s).

These tests deliberately exercise the fixtures themselves rather than the
endpoints, so a broken guard fails here instead of showing up as a slow CI run.
"""

import socket
from unittest.mock import MagicMock, patch

import pytest


class TestSecretsAreCleared:
    """Module-level constants must never carry real .env values into tests."""

    def test_trafiklab_key_is_empty(self):
        """A real TRAFIKLAB_KEY would make /api/vehicles fetch the live feed."""
        import server
        assert server.TRAFIKLAB_KEY == ""

    def test_env_vars_are_cleared(self):
        import os
        assert os.environ.get("TRAFIKLAB_KEY") == ""


class TestOutboundHttpIsBlocked:
    """requests.* on the server module is replaced with safe defaults."""

    def test_post_is_mocked_by_default(self):
        """requests.post must be a mock, never the real function."""
        import requests
        import server
        assert server.requests.post is not requests.api.post
        assert isinstance(server.requests.post, MagicMock)

    def test_unmocked_get_raises_instead_of_calling_out(self):
        """requests.get has no sensible universal fake — it must fail loudly."""
        import server
        with pytest.raises(RuntimeError, match="Unmocked requests.get"):
            server.requests.get("https://example.com", timeout=1)

    def test_unmocked_session_raises_instead_of_calling_out(self):
        """Code calling sess.post() on a Session is not covered by patching requests.post."""
        import server
        with pytest.raises(RuntimeError, match="Unmocked requests.Session"):
            server.requests.Session()

class TestSocketGuard:
    """Last-resort net beneath the requests-level mocks."""

    def test_outbound_connect_is_blocked(self):
        """Any non-local socket connect raises immediately."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(RuntimeError, match="Real network access blocked"):
                s.connect(("93.184.216.34", 80))
        finally:
            s.close()

    def test_dns_lookup_is_blocked(self):
        """Hostname resolution is blocked too — it precedes any connect."""
        with pytest.raises(RuntimeError, match="Real network access blocked"):
            socket.getaddrinfo("example.com", 443)

    def test_localhost_is_still_allowed(self):
        """Loopback must keep working so local test servers are unaffected."""
        assert socket.getaddrinfo("127.0.0.1", 80)


class TestPatchComposition:
    """Per-test @patch must still win over the autouse default."""

    @patch("server.requests.post")
    def test_decorator_patch_overrides_fixture_default(self, mock_post):
        """Decorator-level patches replace the fixture mock and receive the calls."""
        import server
        assert server.requests.post is mock_post
        server.requests.post("https://example.com/hook")
        assert mock_post.called

    def test_fixture_default_restored_after_decorator_patch(self):
        """After a decorated test exits, the fixture mock is back in place."""
        import server
        assert isinstance(server.requests.post, MagicMock)
        resp = server.requests.post("https://example.com/hook")
        assert resp.status_code == 200
