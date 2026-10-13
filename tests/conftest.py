"""Shared fixtures for sl-avgangar tests."""

import os
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure server module is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "", None}


@pytest.fixture(autouse=True)
def _disable_env_secrets(monkeypatch):
    """Clear secrets so tests never call real external APIs."""
    monkeypatch.setenv("TRAFIKLAB_KEY", "")


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    """Last-resort net: fail loudly if any test opens a real outbound socket.

    The mocks below are the primary defence, but they only cover the call
    sites we know about. This fixture makes *any* future unmocked call
    (Trafiklab or any service an extension talks to)
    blow up in milliseconds instead of hanging on a 5-10s timeout. On hosted
    CI runners that difference was ~172s of wall-clock time.
    """
    def _blocked(host):
        raise RuntimeError(
            f"Real network access blocked in tests (host={host!r}). "
            "Mock the call — e.g. @patch('server.requests.post')."
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guard_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOCAL_HOSTS:
            _blocked(host)
        return real_connect(self, address, *args, **kwargs)

    def guard_connect_ex(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOCAL_HOSTS:
            _blocked(host)
        return real_connect_ex(self, address, *args, **kwargs)

    def guard_getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS:
            _blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guard_getaddrinfo)


@pytest.fixture(autouse=True)
def _block_real_outbound_http(monkeypatch, _disable_env_secrets):
    """Default-mock server's HTTP surface so tests never hit live APIs.

    - requests.post defaults to a successful, empty response.
    - requests.get and requests.Session default to raising: there is no
      sensible universal fake (a protobuf feed, an HTML page …), so an
      unmocked call should fail fast and visibly instead of going out.
      requests.Session in particular is NOT covered by patching
      requests.post — code that calls sess.post() uses an instance.

    Tests that care about the call itself still @patch these; unittest.mock
    layers on top of the fixture and restores it on exit.

    `server.requests` is the shared requests module, so this covers optional
    extension modules too. Each loaded extension also gets the chance to
    clear its own secrets through its `pytest_isolate(monkeypatch)` hook.

    Depends explicitly on _disable_env_secrets (rather than relying on
    autouse ordering) so env vars are cleared before this fixture's
    `import server` bakes real .env values into module constants.
    """
    import server

    # Module-level constants are read at import time, so clearing the env
    # vars above is not enough if server was imported earlier (e.g. by a
    # future test module that imports it at collection time).
    monkeypatch.setattr(server, "TRAFIKLAB_KEY", "")

    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {}
    fake_response.raise_for_status = MagicMock()
    monkeypatch.setattr(server.requests, "post", MagicMock(return_value=fake_response))

    def _unmocked(name):
        def _raise(*args, **kwargs):
            raise RuntimeError(
                f"Unmocked requests.{name}() in a test — patch "
                f"'server.requests.{name}' instead of calling out for real."
            )
        return _raise

    monkeypatch.setattr(server.requests, "get", _unmocked("get"))
    monkeypatch.setattr(server.requests, "Session", _unmocked("Session"))

    for ext in server.EXTENSIONS:
        isolate = getattr(ext, "pytest_isolate", None)
        if isolate is not None:
            isolate(monkeypatch)


@pytest.fixture()
def app(tmp_path, _disable_env_secrets):
    """Create a Flask test app with BASE_DIR pointing to tmp_path."""
    import server

    original_base_dir = server.BASE_DIR
    server.BASE_DIR = tmp_path
    # Disable rate limiting in tests
    server.limiter.enabled = False

    yield server.app

    server.BASE_DIR = original_base_dir
    server.limiter.enabled = True


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()
